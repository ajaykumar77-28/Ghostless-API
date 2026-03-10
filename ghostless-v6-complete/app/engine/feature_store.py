"""
Ghostless API — Feature Store (v6)

Centralized layer for loading all signals needed by the scoring pipeline.
Previously, every router and task scattered their own DB/Redis queries.
This caused:
  - Duplicate queries for the same data
  - Inconsistent staleness windows
  - No single place to add caching

The FeatureStore is the ONLY place where pipeline inputs are assembled.
It caches aggressively in Redis and falls back to DB on cache miss.

Usage (in router or task):
    store = FeatureStore(db=db, redis=redis, tenant_id=tenant_id)
    context = await store.load_worker_context(external_id)
    pipeline_result = run_pipeline(context, task_context)
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.pipeline import WorkerContext
from app.models.models import (
    Worker, WorkerScore, FraudEvent, Task, Validation
)
from app.services.fraud import structural_hash
from app.services.scoring import (
    effective_fraud_multiplier,
    simple_fraud_multiplier,
    FRAUD_HALFLIFE_DAYS,
)


# ─── Cache TTLs ───────────────────────────────────────────────────────────────

TTL_WORKER_CONTEXT      = 300   # 5 min — used in hot path (validate)
TTL_FRAUD_MULTIPLIER    = 60    # 1 min — needs to be fresh after fraud events
TTL_PEER_BASELINE       = 600   # 10 min — aggregated across cohort, expensive
TTL_RECENT_HASHES       = 120   # 2 min — dedup window
TTL_IP_WORKERS          = 3600  # 1 hr

_MISSING = object()   # sentinel for "not in cache"


class FeatureStore:
    """
    Loads and caches all features needed by the scoring pipeline.

    Each load_* method:
      1. Tries Redis (fast path)
      2. Falls back to DB query on miss
      3. Writes back to Redis on miss

    All methods are async and accept an optional redis=None parameter.
    When redis is None or unavailable, DB is used directly.
    """

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: str,
        redis=None,                    # redis.asyncio.Redis or None
        max_tasks_per_hour: int = 60,
        max_tasks_per_day:  int = 300,
    ):
        self.db             = db
        self.redis          = redis
        self.tenant_id      = tenant_id
        self.max_tasks_per_hour = max_tasks_per_hour
        self.max_tasks_per_day  = max_tasks_per_day

    # ── Internal cache helpers ────────────────────────────────────────────────

    async def _get(self, key: str) -> Any:
        if self.redis is None:
            return _MISSING
        try:
            raw = await self.redis.get(key)
            return json.loads(raw) if raw else _MISSING
        except Exception:
            return _MISSING

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception:
            pass

    async def _del(self, key: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(key)
        except Exception:
            pass

    # ── Worker + Score base ────────────────────────────────────────────────────

    async def load_worker_and_score(
        self, external_id: str
    ) -> tuple[Optional[Worker], Optional[WorkerScore]]:
        """Load Worker + WorkerScore rows from DB (no cache — used on write path)."""
        result = await self.db.execute(
            select(Worker, WorkerScore)
            .outerjoin(WorkerScore, WorkerScore.worker_id == Worker.id)
            .where(Worker.external_id == external_id)
            .where(Worker.tenant_id   == self.tenant_id)
        )
        row = result.first()
        if not row:
            return None, None
        return row[0], row[1]

    # ── Fraud multiplier (with decay) ─────────────────────────────────────────

    async def load_fraud_multiplier(self, worker_id: str) -> float:
        cache_key = f"fraud_mult:{self.tenant_id}:{worker_id}"
        cached = await self._get(cache_key)
        if cached is not _MISSING:
            return float(cached)

        result = await self.db.execute(
            select(FraudEvent.created_at)
            .where(FraudEvent.worker_id == worker_id)
            .where(FraudEvent.tenant_id == self.tenant_id)
            .where(FraudEvent.reviewed  == False)
            .order_by(FraudEvent.created_at.desc())
            .limit(10)
        )
        rows = result.scalars().all()

        now = datetime.now(timezone.utc)
        fraud_events_aged = [
            {"age_days": (now - ts).total_seconds() / 86400}
            for ts in rows
        ]

        multiplier = effective_fraud_multiplier(fraud_events_aged)
        await self._set(cache_key, multiplier, TTL_FRAUD_MULTIPLIER)
        return multiplier

    # ── Velocity counters (from Redis; DB fallback) ───────────────────────────

    async def load_velocity(self, external_id: str) -> tuple[int, int]:
        """Returns (tasks_last_hour, tasks_last_day)."""
        if self.redis is None:
            # DB fallback: count recent tasks
            result_hr = await self.db.execute(
                select(func.count())
                .select_from(Task)
                .join(Worker, Worker.id == Task.worker_id)
                .where(Worker.external_id == external_id)
                .where(Worker.tenant_id   == self.tenant_id)
                .where(Task.submitted_at  >= text("NOW() - INTERVAL '1 hour'"))
            )
            result_day = await self.db.execute(
                select(func.count())
                .select_from(Task)
                .join(Worker, Worker.id == Task.worker_id)
                .where(Worker.external_id == external_id)
                .where(Worker.tenant_id   == self.tenant_id)
                .where(Task.submitted_at  >= text("NOW() - INTERVAL '1 day'"))
            )
            return result_hr.scalar() or 0, result_day.scalar() or 0

        try:
            hr_key  = f"vel:hr:{self.tenant_id}:{external_id}"
            day_key = f"vel:day:{self.tenant_id}:{external_id}"
            hr      = int(await self.redis.get(hr_key)  or 0)
            day     = int(await self.redis.get(day_key) or 0)
            return hr, day
        except Exception:
            return 0, 0

    # ── Recent payload hashes (anti-farming + dedup) ──────────────────────────

    async def load_recent_hashes(
        self, worker_id: str, limit: int = 20
    ) -> List[str]:
        cache_key = f"hashes:{self.tenant_id}:{worker_id}"
        cached = await self._get(cache_key)
        if cached is not _MISSING:
            return cached

        result = await self.db.execute(
            select(Task.metadata_)
            .where(Task.worker_id == worker_id)
            .where(Task.tenant_id == self.tenant_id)
            .order_by(Task.submitted_at.desc())
            .limit(limit)
        )
        hashes = [
            row["payload_hash"]
            for row in (result.scalars().all() or [])
            if isinstance(row, dict) and "payload_hash" in row
        ]
        await self._set(cache_key, hashes, TTL_RECENT_HASHES)
        return hashes

    # ── Recent task types + accepted flags (anti-farming) ─────────────────────

    async def load_recent_actions(
        self, worker_id: str, limit: int = 20
    ) -> tuple[List[str], List[bool]]:
        cache_key = f"actions:{self.tenant_id}:{worker_id}"
        cached = await self._get(cache_key)
        if cached is not _MISSING:
            return cached["types"], cached["accepted"]

        result = await self.db.execute(
            select(Task.task_type, Task.was_accepted)
            .where(Task.worker_id == worker_id)
            .where(Task.tenant_id == self.tenant_id)
            .where(Task.was_accepted.isnot(None))
            .order_by(Task.submitted_at.desc())
            .limit(limit)
        )
        rows = result.all()
        types    = [r[0] for r in rows]
        accepted = [bool(r[1]) for r in rows]
        await self._set(cache_key, {"types": types, "accepted": accepted}, 120)
        return types, accepted

    # ── IP workers (Sybil detection) ──────────────────────────────────────────

    async def load_ip_workers(self, ip_address: str) -> List[str]:
        if not ip_address or ip_address in ("127.0.0.1", "::1"):
            return []
        cache_key = f"ip_workers:{self.tenant_id}:{ip_address}"
        if self.redis:
            try:
                members = await self.redis.smembers(cache_key)
                return list(members)
            except Exception:
                pass
        return []

    # ── Peer baseline (cohort z-score) ────────────────────────────────────────

    async def load_peer_zscore(
        self,
        worker_trust: float,
        task_type: str,
    ) -> tuple[Optional[float], int]:
        """
        Returns (z_score, peer_n) for worker_trust vs. same-task-type cohort.
        Uses an approximate running mean/std stored in Redis.
        Falls back to DB aggregate on miss.
        """
        cache_key = f"peer_baseline:{self.tenant_id}:{task_type}"
        cached = await self._get(cache_key)
        if cached is not _MISSING:
            mean = cached.get("mean", 50.0)
            std  = cached.get("std", 10.0)
            n    = cached.get("n", 0)
        else:
            # DB: compute mean and stddev of trust scores for this task_type
            result = await self.db.execute(
                text("""
                    SELECT
                        AVG(ws.trust_score)    AS mean,
                        STDDEV(ws.trust_score) AS std,
                        COUNT(*)               AS n
                    FROM worker_scores ws
                    JOIN workers w ON w.id = ws.worker_id
                    JOIN tasks t   ON t.worker_id = w.id
                    WHERE w.tenant_id = :tenant_id
                      AND t.task_type = :task_type
                      AND t.submitted_at >= NOW() - INTERVAL '7 days'
                """),
                {"tenant_id": self.tenant_id, "task_type": task_type},
            )
            row  = result.first()
            mean = float(row[0] or 50.0)
            std  = float(row[1] or 10.0)
            n    = int(row[2] or 0)

            await self._set(
                cache_key,
                {"mean": mean, "std": std, "n": n},
                TTL_PEER_BASELINE,
            )

        if n < 10 or std < 1e-6:
            return None, n

        zscore = (worker_trust - mean) / std
        return round(zscore, 3), n

    # ── Full WorkerContext assembly ────────────────────────────────────────────

    async def load_worker_context(
        self,
        external_id: str,
        task_type:   str = "default",
        ip_address:  str = "",
    ) -> WorkerContext:
        """
        One-stop shop: loads all features for a worker and returns a
        WorkerContext ready for run_pipeline().
        """
        worker, score = await self.load_worker_and_score(external_id)

        if worker is None:
            # New worker — return default context
            return WorkerContext(
                external_id = external_id,
                ip_address  = ip_address,
            )

        worker_id_str = str(worker.id)

        # Parallel loads (all are awaitable but we sequence here for simplicity;
        # in production, use asyncio.gather for speed)
        fraud_mult        = await self.load_fraud_multiplier(worker_id_str)
        tasks_hr, tasks_day = await self.load_velocity(external_id)
        recent_hashes     = await self.load_recent_hashes(worker_id_str)
        recent_types, recent_acc = await self.load_recent_actions(worker_id_str)
        ip_workers        = await self.load_ip_workers(ip_address)

        trust = score.trust_score if score else 50.0
        zscore, peer_n = await self.load_peer_zscore(trust, task_type)

        # Derive days_since_joined
        now = datetime.now(timezone.utc)
        created = worker.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days_since = (now - created).days if created else 0.0

        # Posterior parameters (stored in WorkerScore.task_baselines as fallback)
        # In v6 we store these on WorkerScore; read from metadata if present
        baselines = (score.task_baselines or {}) if score else {}
        posterior = baselines.get("__bayesian__", {})
        alpha = posterior.get("alpha", 2.0)
        beta  = posterior.get("beta",  2.0)

        return WorkerContext(
            external_id          = external_id,
            trust_score          = trust,
            posterior_alpha      = alpha,
            posterior_beta       = beta,
            total_tasks          = score.total_tasks if score else 0,
            streak_days          = score.streak_days if score else 0,
            days_since_joined    = float(days_since),
            max_trust            = score.max_trust if score else 100.0,
            shadow_banned        = score.shadow_banned if score else False,
            lifecycle_stage      = score.lifecycle_stage.value if (score and score.lifecycle_stage) else "new",
            volatility           = 0.0,   # TODO: store in WorkerScore in migration
            fraud_multiplier     = fraud_mult,
            recent_task_types    = recent_types,
            recent_accepted      = recent_acc,
            recent_payload_hashes = recent_hashes,
            tasks_last_hour      = tasks_hr,
            tasks_last_day       = tasks_day,
            max_tasks_per_hour   = self.max_tasks_per_hour,
            max_tasks_per_day    = self.max_tasks_per_day,
            peer_zscore          = zscore,
            peer_n               = peer_n,
            ip_address           = ip_address,
            other_workers_on_ip  = ip_workers,
        )

    # ── Cache invalidation ────────────────────────────────────────────────────

    async def invalidate_worker(self, external_id: str, worker_id: str) -> None:
        """Call after any write that changes worker features."""
        keys = [
            f"worker_ctx:{self.tenant_id}:{external_id}",
            f"fraud_mult:{self.tenant_id}:{worker_id}",
            f"hashes:{self.tenant_id}:{worker_id}",
            f"actions:{self.tenant_id}:{worker_id}",
        ]
        for key in keys:
            await self._del(key)

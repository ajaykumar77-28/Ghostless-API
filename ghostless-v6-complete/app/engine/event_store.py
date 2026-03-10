"""
Ghostless API — Event Store (v6)

Immutable append-only behavior log. Every significant state change in the
system is recorded as an event here before (or alongside) the DB mutation.

Why event sourcing here?
  - Current system mutates WorkerScore in-place → no history of how trust
    reached its current value without reading ScoringDecisionLog + DB.
  - With an event stream, you can replay all events to reconstruct any
    past state — invaluable for audits and fraud investigations.
  - The event stream (Redis Streams + Postgres JSONB archive) acts as a
    lightweight saga log for cross-service coordination.

Event categories:
  - scoring.updated         — every trust score change
  - fraud.signal_detected   — every FraudSignal emitted by the pipeline
  - fraud.action_taken      — suspend, flag, shadow_ban, ban
  - ledger.entry_created    — every ledger mutation
  - worker.lifecycle_changed — stage transitions
  - baseline.drift_detected  — when Welford baseline shifts significantly
  - appeal.submitted / appeal.resolved

Architecture:
  - Events are written to a Redis Stream (stream:events:<tenant_id>)
    with maxlen=500_000 — this is the hot path (fast).
  - A background task (tasks/event_archiver.py) drains the stream into
    the `behavior_events` Postgres table for long-term retention.
  - Callers use `emit()` and do not care about persistence details.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


# ─── Event envelope ───────────────────────────────────────────────────────────

@dataclass
class BehaviorEvent:
    event_id:     str
    event_type:   str            # e.g. "scoring.updated"
    tenant_id:    str
    worker_id:    Optional[str]  # DB UUID, or None for system events
    payload:      Dict[str, Any]
    algorithm_version: str = "v6.0.0"
    ts:           float = field(default_factory=time.time)

    def to_redis_fields(self) -> Dict[str, str]:
        """Flatten for Redis XADD (all values must be strings)."""
        return {
            "event_id":          self.event_id,
            "event_type":        self.event_type,
            "tenant_id":         self.tenant_id,
            "worker_id":         self.worker_id or "",
            "payload":           json.dumps(self.payload),
            "algorithm_version": self.algorithm_version,
            "ts":                str(self.ts),
        }


# ─── Event factory helpers ────────────────────────────────────────────────────

def scoring_updated_event(
    tenant_id:        str,
    worker_id:        str,
    trust_before:     float,
    trust_after:      float,
    explanation:      Dict[str, Any],
    algorithm_version: str = "v6.0.0",
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "scoring.updated",
        tenant_id  = tenant_id,
        worker_id  = worker_id,
        algorithm_version = algorithm_version,
        payload    = {
            "trust_before":  trust_before,
            "trust_after":   trust_after,
            "delta":         round(trust_after - trust_before, 4),
            **explanation,
        },
    )


def fraud_signal_event(
    tenant_id:    str,
    worker_id:    str,
    signal_type:  str,
    severity:     str,
    details:      Dict[str, Any],
    auto_action:  Optional[str] = None,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "fraud.signal_detected",
        tenant_id  = tenant_id,
        worker_id  = worker_id,
        payload    = {
            "signal_type": signal_type,
            "severity":    severity,
            "details":     details,
            "auto_action": auto_action,
        },
    )


def fraud_action_event(
    tenant_id:  str,
    worker_id:  str,
    action:     str,    # "flag" | "shadow_ban" | "suspend" | "ban"
    reason:     str,
    performed_by: str = "system",
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "fraud.action_taken",
        tenant_id  = tenant_id,
        worker_id  = worker_id,
        payload    = {
            "action":       action,
            "reason":       reason,
            "performed_by": performed_by,
        },
    )


def ledger_event(
    tenant_id:      str,
    worker_id:      str,
    entry_type:     str,
    amount:         float,
    currency:       str,
    running_balance: float,
    idempotency_key: str,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "ledger.entry_created",
        tenant_id  = tenant_id,
        worker_id  = worker_id,
        payload    = {
            "entry_type":      entry_type,
            "amount":          amount,
            "currency":        currency,
            "running_balance": running_balance,
            "idempotency_key": idempotency_key,
        },
    )


def lifecycle_event(
    tenant_id:  str,
    worker_id:  str,
    from_stage: str,
    to_stage:   str,
    reason:     str,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "worker.lifecycle_changed",
        tenant_id  = tenant_id,
        worker_id  = worker_id,
        payload    = {
            "from_stage": from_stage,
            "to_stage":   to_stage,
            "reason":     reason,
        },
    )


def baseline_drift_event(
    tenant_id:  str,
    task_type:  str,
    delta_sigma: float,
    direction:  str,
    snap_mean:  float,
    curr_mean:  float,
) -> BehaviorEvent:
    return BehaviorEvent(
        event_id   = f"ev_{uuid.uuid4().hex}",
        event_type = "baseline.drift_detected",
        tenant_id  = tenant_id,
        worker_id  = None,
        payload    = {
            "task_type":   task_type,
            "delta_sigma": delta_sigma,
            "direction":   direction,
            "snap_mean":   snap_mean,
            "curr_mean":   curr_mean,
        },
    )


# ─── Emitter ──────────────────────────────────────────────────────────────────

class EventStore:
    """
    Thin wrapper around Redis Streams for event emission.

    Usage:
        store = EventStore(redis=redis_client)
        event = scoring_updated_event(...)
        await store.emit(event)
    """

    STREAM_PREFIX = "stream:events"
    STREAM_MAXLEN = 500_000

    def __init__(self, redis=None):
        self.redis = redis

    async def emit(self, event: BehaviorEvent) -> Optional[str]:
        """
        Write event to Redis Stream. Returns stream message ID or None.
        Failures are swallowed — event emission must never block the hot path.
        """
        if self.redis is None:
            return None
        stream_key = f"{self.STREAM_PREFIX}:{event.tenant_id}"
        try:
            msg_id = await self.redis.xadd(
                stream_key,
                event.to_redis_fields(),
                maxlen=self.STREAM_MAXLEN,
                approximate=True,
            )
            return msg_id
        except Exception:
            return None

    async def emit_many(self, events: list[BehaviorEvent]) -> None:
        """Emit a batch of events. Uses pipeline for efficiency."""
        if self.redis is None or not events:
            return
        try:
            pipe = self.redis.pipeline()
            for event in events:
                stream_key = f"{self.STREAM_PREFIX}:{event.tenant_id}"
                pipe.xadd(
                    stream_key,
                    event.to_redis_fields(),
                    maxlen=self.STREAM_MAXLEN,
                    approximate=True,
                )
            await pipe.execute()
        except Exception:
            pass

    async def read_recent(
        self,
        tenant_id: str,
        count: int = 100,
        last_id: str = "0",
    ) -> list[Dict[str, Any]]:
        """Read recent events for a tenant (used by admin/debug endpoints)."""
        if self.redis is None:
            return []
        stream_key = f"{self.STREAM_PREFIX}:{tenant_id}"
        try:
            messages = await self.redis.xread(
                {stream_key: last_id},
                count=count,
                block=0,
            )
            if not messages:
                return []
            results = []
            for _, msgs in messages:
                for msg_id, fields in msgs:
                    results.append({
                        "id":     msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                        "fields": {
                            k.decode() if isinstance(k, bytes) else k:
                            v.decode() if isinstance(v, bytes) else v
                            for k, v in fields.items()
                        },
                    })
            return results
        except Exception:
            return []

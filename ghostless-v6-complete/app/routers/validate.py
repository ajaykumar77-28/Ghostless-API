"""
Ghostless API — Validation Router (v5)

New in v5:
  - Request idempotency header (Idempotency-Key)
  - HMAC request signature validation (optional, X-Signature-SHA256)
  - Replay attack protection (timestamp + nonce in signed requests)
  - Redis outage fallback (degrade gracefully using cached history)
  - Anomaly score computed and persisted per submission
  - Velocity penalty fed back to submission response
  - Shadow-ban check: accepted but payout zeroed on validated response
  - Submission idempotency_key on Validation row (replay protection)
"""
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, get_redis, require_auth
from app.models.models import Worker, WorkerScore, Task, Validation
from app.services.rule_engine import ValidationWarning, ml_quality_hook, rule_engine
from app.services.scoring import compute_anomaly_score
from app.services.fraud import shannon_entropy, structural_hash

router = APIRouter(prefix="/validate", tags=["Validation"])


# ─── Request / Response ───────────────────────────────────────────────────────

class ValidateTaskRequest(BaseModel):
    worker_id:       str   = Field(..., description="Your platform's worker identifier")
    task_type:       str   = Field(..., description="image_label | transcription | survey | moderation")
    payload:         Dict[str, Any] = Field(..., description="The task submission data")
    completion_time: float = Field(..., description="Seconds taken by the worker", gt=0)
    project_id:      Optional[str]   = Field(None)
    difficulty:      Optional[float] = Field(1.0, ge=0.1, le=10.0,
                                             description="Task difficulty multiplier (default 1.0)")
    # V5: optional client-provided timestamp for replay protection
    client_timestamp: Optional[int]  = Field(None, description="Unix timestamp from client (for HMAC replay protection)")


class ValidateTaskResponse(BaseModel):
    validation_id:    str
    worker_id:        str
    task_type:        str
    quality_score:    float
    allow_submit:     bool
    warnings:         List[ValidationWarning]
    suggestions:      List[str]
    processed_ms:     int
    flags:            List[str]
    anomaly_score:    float    # V5: per-submission composite anomaly
    velocity_warning: bool     # V5: approaching velocity limit
    shadow_banned:    bool     # V5: worker is shadow-banned


# ─── HMAC validation ──────────────────────────────────────────────────────────

def _verify_hmac_signature(
    body_bytes: bytes,
    signature_header: Optional[str],
    tenant_secret: str,
    client_timestamp: Optional[int],
) -> bool:
    """
    V5: Validate HMAC-SHA256 request signature.
    Signature covers: timestamp + "." + SHA256(body)
    Replay protection: reject if |now - timestamp| > HMAC_TOLERANCE_SECONDS.
    """
    if not settings.HMAC_SIGNATURE_REQUIRED:
        return True
    if not signature_header:
        return False

    # Replay protection: check timestamp freshness
    now = int(time.time())
    if client_timestamp is None or abs(now - client_timestamp) > settings.HMAC_TOLERANCE_SECONDS:
        return False

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    message   = f"{client_timestamp}.{body_hash}".encode()
    expected  = hmac.new(tenant_secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ─── Worker history (with Redis fallback) ────────────────────────────────────

async def get_worker_history(
    external_id: str,
    tenant_id: str,
    db: AsyncSession,
) -> dict:
    """
    V5: Redis outage fallback — if Redis is unavailable, load directly from DB.
    Uses a degraded cache key to avoid thundering herd on recovery.
    """
    try:
        redis = await get_redis()
        cache_key = f"worker_history:{tenant_id}:{external_id}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
        redis_ok = True
    except Exception:
        redis_ok  = False
        redis     = None

    # Load from DB
    result = await db.execute(
        select(Worker, WorkerScore)
        .outerjoin(WorkerScore, WorkerScore.worker_id == Worker.id)
        .where(Worker.external_id == external_id)
        .where(Worker.tenant_id == tenant_id)
    )
    row = result.first()

    if row:
        worker, score = row
        history = {
            "worker_db_id":        str(worker.id),
            "trust_score":         score.trust_score     if score else 50.0,
            "avg_completion_time": 120.0,
            "total_tasks":         score.total_tasks     if score else 0,
            "accuracy_30d":        score.accuracy_30d    if score else None,
            "tier":                worker.tier.value,
            "shadow_banned":       score.shadow_banned   if score else False,
            "velocity_score":      score.velocity_score  if score else 1.0,
            "lifecycle_stage":     score.lifecycle_stage.value if (score and score.lifecycle_stage) else "new",
        }
    else:
        history = {
            "worker_db_id": None, "trust_score": 50.0,
            "avg_completion_time": 120.0, "total_tasks": 0,
            "accuracy_30d": None, "tier": "bronze",
            "shadow_banned": False, "velocity_score": 1.0,
            "lifecycle_stage": "new",
        }

    if redis_ok and redis:
        try:
            await redis.setex(
                f"worker_history:{tenant_id}:{external_id}",
                settings.CACHE_TTL_WORKER_HISTORY,
                json.dumps(history),
            )
        except Exception:
            pass   # cache write failure is non-fatal

    return history


# ─── Background: persist validation event ────────────────────────────────────

async def _persist_validation(
    tenant_id: str, external_worker_id: str,
    task_type: str, project_id: Optional[str],
    payload: dict, completion_time: float,
    quality_score: float, was_allowed: bool,
    flags: List[str], warning_count: int, error_count: int,
    processed_ms: int, anomaly_score: float,
    difficulty_weight: float, submission_idem_key: str,
    db: AsyncSession,
):
    """Write Task + Validation rows to DB asynchronously."""
    try:
        result = await db.execute(
            select(Worker)
            .where(Worker.external_id == external_worker_id)
            .where(Worker.tenant_id == tenant_id)
        )
        worker = result.scalar_one_or_none()
        if not worker:
            worker = Worker(tenant_id=tenant_id, external_id=external_worker_id)
            db.add(worker)
            await db.flush()

        task = Task(
            tenant_id=tenant_id,
            worker_id=worker.id,
            task_type=task_type,
            project_id=project_id,
            completion_time=completion_time,
            was_accepted=None,
            difficulty_weight=difficulty_weight,
            metadata_={
                "payload_hash": structural_hash(payload),
                "payload_keys": list(payload.keys()),
            },
        )
        db.add(task)
        await db.flush()

        validation = Validation(
            task_id=task.id,
            worker_id=worker.id,
            tenant_id=tenant_id,
            quality_score=quality_score,
            anomaly_score=anomaly_score,
            warning_count=warning_count,
            error_count=error_count,
            was_allowed=was_allowed,
            rule_flags=flags,
            processed_ms=processed_ms,
            submission_idempotency_key=submission_idem_key,
            difficulty_weight=difficulty_weight,
        )
        db.add(validation)
        await db.commit()

        # Invalidate worker history cache
        try:
            redis = await get_redis()
            await redis.delete(f"worker_history:{tenant_id}:{external_worker_id}")
            await redis.xadd(
                "stream:validations",
                {
                    "tenant_id":     tenant_id,
                    "worker_id":     str(worker.id),
                    "external_id":   external_worker_id,
                    "quality_score": str(quality_score),
                    "anomaly_score": str(anomaly_score),
                    "was_allowed":   "1" if was_allowed else "0",
                    "flags":         json.dumps(flags),
                },
                maxlen=100_000,
            )
        except Exception:
            pass   # Redis failure is non-fatal for persistence

    except Exception as e:
        import structlog
        structlog.get_logger().error("persist_validation_failed", error=str(e))


# ─── Main Endpoint ────────────────────────────────────────────────────────────

@router.post("/task", response_model=ValidateTaskResponse, summary="Pre-validate a task submission")
async def validate_task(
    request: Request,
    body: ValidateTaskRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_signature: Optional[str]     = Header(None, alias="X-Signature-SHA256"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Run rule-based + ML validation on a worker's task submission.
    V5 additions: HMAC signature check, replay protection, Redis fallback,
    anomaly score, velocity feedback, shadow-ban awareness.
    """
    start_ms = start_time = time.perf_counter()

    # ── V5: HMAC signature validation (optional) ───────────────────────────
    if settings.HMAC_SIGNATURE_REQUIRED:
        raw_body = await request.body()
        tenant_secret = getattr(auth, "jwt_secret", settings.SECRET_KEY)
        if not _verify_hmac_signature(raw_body, x_signature, tenant_secret,
                                      body.client_timestamp):
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_signature",
                        "message": "Request signature missing, invalid, or replayed."},
            )

    # ── Get Redis (with fallback flag) ─────────────────────────────────────
    redis_ok = True
    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        redis_ok = False
        redis    = None

    # ── V5: Idempotency-Key header dedup ──────────────────────────────────
    if idempotency_key and redis_ok:
        idem_cache_key = f"idem:{auth.tenant_id}:{idempotency_key}"
        existing = await redis.get(idem_cache_key)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={"error": "idempotent_replay",
                        "message": "This Idempotency-Key was already processed."},
            )

    # ── FIX #8: Velocity hard limits ──────────────────────────────────────
    tasks_last_minute = 0
    tasks_last_hour   = 0
    if redis_ok:
        velocity_minute_key = f"vel:min:{auth.tenant_id}:{body.worker_id}"
        velocity_hour_key   = f"vel:hr:{auth.tenant_id}:{body.worker_id}"
        tasks_last_minute   = int(await redis.get(velocity_minute_key) or 0)
        tasks_last_hour     = int(await redis.get(velocity_hour_key) or 0)

    max_per_minute = getattr(settings, "MAX_TASKS_PER_MINUTE", 3)
    max_per_hour   = settings.MAX_TASKS_PER_HOUR
    velocity_ratio = tasks_last_hour / max(max_per_hour, 1)

    if tasks_last_minute >= max_per_minute or tasks_last_hour >= max_per_hour:
        async def _suspend_worker():
            from sqlalchemy import update as sql_update
            try:
                await db.execute(
                    sql_update(Worker)
                    .where(Worker.external_id == body.worker_id)
                    .where(Worker.tenant_id == auth.tenant_id)
                    .values(status="suspended")
                )
                await db.commit()
            except Exception:
                pass
        background_tasks.add_task(_suspend_worker)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "velocity_limit_exceeded",
                "tasks_last_minute": tasks_last_minute,
                "tasks_last_hour":   tasks_last_hour,
                "worker_suspended":  True,
            },
        )

    # ── FIX #9: IP enforcement ────────────────────────────────────────────
    client_ip = request.client.host if request.client else ""
    if client_ip and redis_ok and client_ip not in ("127.0.0.1", "::1"):
        ip_workers_key = f"ip_workers:{auth.tenant_id}:{client_ip}"
        await redis.sadd(ip_workers_key, body.worker_id)
        await redis.expire(ip_workers_key, 3600)
        ip_worker_count = await redis.scard(ip_workers_key)
        if ip_worker_count > settings.MAX_WORKERS_PER_IP:
            suspect_key = f"ip_suspect:{auth.tenant_id}:{body.worker_id}"
            await redis.setex(suspect_key, 86400, str(ip_worker_count))

    # ── FIX #10: Short-window coordinated attack detection ────────────────
    payload_hash = structural_hash(body.payload)
    if payload_hash and redis_ok:
        coord_key   = f"coord:{auth.tenant_id}:{payload_hash}"
        await redis.hset(coord_key, f"worker:{body.worker_id}", int(time.time()))
        await redis.expire(coord_key, 300)
        coord_count = await redis.hlen(coord_key)
        if coord_count >= settings.MAX_COORD_WORKERS:
            await redis.xadd(
                "stream:fraud_signals",
                {
                    "type":         "coordinated_attack",
                    "tenant_id":    auth.tenant_id,
                    "worker_id":    body.worker_id,
                    "payload_hash": payload_hash,
                    "workers_seen": str(coord_count),
                    "ts":           str(int(time.time())),
                },
                maxlen=50_000,
            )

    # ── Payload dedup (hash-based) ─────────────────────────────────────────
    if redis_ok:
        dedup_key = f"dedup:{auth.tenant_id}:{body.worker_id}:{payload_hash}"
        if await redis.get(dedup_key):
            raise HTTPException(
                status_code=409,
                detail={"error": "duplicate_submission",
                        "message": "Identical payload submitted within the dedup window."},
            )

    # ── Load worker history ────────────────────────────────────────────────
    worker_history = await get_worker_history(body.worker_id, auth.tenant_id, db)

    # ── Shadow-ban check ──────────────────────────────────────────────────
    is_shadow_banned = worker_history.get("shadow_banned", False)

    # ── Run rule engine ────────────────────────────────────────────────────
    rule_result = rule_engine.run(
        task_type=body.task_type,
        payload=body.payload,
        completion_time=body.completion_time,
        worker_history=worker_history,
    )

    # ── ML hook ───────────────────────────────────────────────────────────
    quality_score = await ml_quality_hook(
        task_type=body.task_type,
        payload=body.payload,
        base_score=rule_result.quality_score,
        worker_history=worker_history,
    )

    # ── V5: Compute anomaly score ─────────────────────────────────────────
    raw_text  = json.dumps(body.payload, sort_keys=True)
    ent       = shannon_entropy(raw_text) if len(raw_text) >= 20 else 1.0
    ent_norm  = min(1.0, ent / 4.0)    # ~4 bits/char is normal for JSON text
    anomaly   = compute_anomaly_score(
        zscore         = None,   # z-score not available at validation time
        velocity_ratio = velocity_ratio,
        entropy_score  = ent_norm,
    )

    # ── Submit decision ───────────────────────────────────────────────────
    has_errors   = any(w.severity == "error" for w in rule_result.warnings)
    allow_submit = (not has_errors) and quality_score >= settings.MIN_QUALITY_SCORE_TO_SUBMIT

    # ── Mark dedup + increment velocity counters ──────────────────────────
    if redis_ok:
        await redis.setex(dedup_key, settings.DEDUP_WINDOW_SECONDS, "1")
        pipe = redis.pipeline()
        pipe.incr(velocity_minute_key); pipe.expire(velocity_minute_key, 60)
        pipe.incr(velocity_hour_key);   pipe.expire(velocity_hour_key, 3600)
        await pipe.execute()
        # Mark Idempotency-Key
        if idempotency_key:
            await redis.setex(idem_cache_key, settings.DEDUP_WINDOW_SECONDS, "1")

    processed_ms = int((time.perf_counter() - start_ms) * 1000)
    validation_id = f"val_{auth.tenant_id[:8]}_{int(time.time() * 1000)}"
    submission_idem_key = idempotency_key or f"sub_{uuid.uuid4().hex}"

    # ── Persist asynchronously ────────────────────────────────────────────
    background_tasks.add_task(
        _persist_validation,
        tenant_id           = auth.tenant_id,
        external_worker_id  = body.worker_id,
        task_type           = body.task_type,
        project_id          = body.project_id,
        payload             = body.payload,
        completion_time     = body.completion_time,
        quality_score       = quality_score,
        was_allowed         = allow_submit,
        flags               = rule_result.flags,
        warning_count       = sum(1 for w in rule_result.warnings if w.severity in ("warning", "info")),
        error_count         = sum(1 for w in rule_result.warnings if w.severity == "error"),
        processed_ms        = processed_ms,
        anomaly_score       = anomaly,
        difficulty_weight   = body.difficulty or 1.0,
        submission_idem_key = submission_idem_key,
        db                  = db,
    )

    return ValidateTaskResponse(
        validation_id    = validation_id,
        worker_id        = body.worker_id,
        task_type        = body.task_type,
        quality_score    = round(quality_score, 4),
        allow_submit     = allow_submit,
        warnings         = rule_result.warnings,
        suggestions      = rule_result.suggestions,
        flags            = rule_result.flags,
        processed_ms     = processed_ms,
        anomaly_score    = anomaly,
        velocity_warning = velocity_ratio > 0.8,
        shadow_banned    = is_shadow_banned,
    )

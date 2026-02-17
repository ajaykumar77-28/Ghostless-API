"""
Ghostless API — Validation Router

POST /v1/validate/task
  Pre-validate a worker's submission before it's sent to the client platform.
  Returns quality score, warnings, suggestions, and submit decision.
"""
import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, get_redis, require_auth
from app.models.models import Worker, WorkerScore, Task, Validation
from app.services.rule_engine import ValidationWarning, ml_quality_hook, rule_engine

router = APIRouter(prefix="/validate", tags=["Validation"])


# ─── Request / Response ───────────────────────────────────────────────────────

class ValidateTaskRequest(BaseModel):
    worker_id:       str = Field(..., description="Your platform's worker identifier")
    task_type:       str = Field(..., description="image_label | transcription | survey | moderation")
    payload:         Dict[str, Any] = Field(..., description="The task submission data")
    completion_time: float = Field(..., description="Seconds taken by the worker", gt=0)
    project_id:      Optional[str] = Field(None, description="Optional project context")


class ValidateTaskResponse(BaseModel):
    validation_id:  str
    worker_id:      str
    task_type:      str
    quality_score:  float
    allow_submit:   bool
    warnings:       List[ValidationWarning]
    suggestions:    List[str]
    processed_ms:   int
    flags:          List[str]


# ─── Worker history (cached) ──────────────────────────────────────────────────

async def get_worker_history(
    external_id: str,
    tenant_id: str,
    db: AsyncSession,
) -> dict:
    redis = await get_redis()
    cache_key = f"worker_history:{tenant_id}:{external_id}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

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
            "worker_db_id":       str(worker.id),
            "trust_score":        score.trust_score if score else 50.0,
            "avg_completion_time": 120.0,           # TODO: compute from recent tasks
            "total_tasks":        score.total_tasks if score else 0,
            "accuracy_30d":       score.accuracy_30d if score else None,
            "tier":               worker.tier.value,
        }
    else:
        # New worker — use defaults
        history = {
            "worker_db_id":       None,
            "trust_score":        50.0,
            "avg_completion_time": 120.0,
            "total_tasks":        0,
            "accuracy_30d":       None,
            "tier":               "bronze",
        }

    await redis.setex(cache_key, settings.CACHE_TTL_WORKER_HISTORY, json.dumps(history))
    return history


# ─── Background: persist validation event ────────────────────────────────────

async def _persist_validation(
    tenant_id: str,
    external_worker_id: str,
    task_type: str,
    project_id: Optional[str],
    payload: dict,
    completion_time: float,
    quality_score: float,
    was_allowed: bool,
    flags: List[str],
    warning_count: int,
    error_count: int,
    processed_ms: int,
    db: AsyncSession,
):
    """Write Task + Validation rows to the DB asynchronously."""
    try:
        # Get or create worker
        result = await db.execute(
            select(Worker)
            .where(Worker.external_id == external_worker_id)
            .where(Worker.tenant_id == tenant_id)
        )
        worker = result.scalar_one_or_none()

        if not worker:
            worker = Worker(
                tenant_id=tenant_id,
                external_id=external_worker_id,
            )
            db.add(worker)
            await db.flush()

        # Create task record
        task = Task(
            tenant_id=tenant_id,
            worker_id=worker.id,
            task_type=task_type,
            project_id=project_id,
            completion_time=completion_time,
            was_accepted=None,  # Pending — client platform accepts/rejects later
            metadata_={"payload_keys": list(payload.keys())},  # Don't store raw PII
        )
        db.add(task)
        await db.flush()

        # Create validation record
        validation = Validation(
            task_id=task.id,
            worker_id=worker.id,
            tenant_id=tenant_id,
            quality_score=quality_score,
            warning_count=warning_count,
            error_count=error_count,
            was_allowed=was_allowed,
            rule_flags=flags,
            processed_ms=processed_ms,
        )
        db.add(validation)
        await db.commit()

        # Invalidate worker history cache
        redis = await get_redis()
        await redis.delete(f"worker_history:{tenant_id}:{external_worker_id}")

        # Push to stream for scoring engine to pick up
        await redis.xadd(
            "stream:validations",
            {
                "tenant_id":    tenant_id,
                "worker_id":    str(worker.id),
                "external_id":  external_worker_id,
                "quality_score": str(quality_score),
                "was_allowed":  "1" if was_allowed else "0",
                "flags":        json.dumps(flags),
            },
            maxlen=100_000,
        )

    except Exception as e:
        import structlog
        structlog.get_logger().error("persist_validation_failed", error=str(e))


# ─── Main Endpoint ────────────────────────────────────────────────────────────

@router.post("/task", response_model=ValidateTaskResponse, summary="Pre-validate a task submission")
async def validate_task(
    body: ValidateTaskRequest,
    background_tasks: BackgroundTasks,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Run rule-based + ML validation on a worker's task submission.

    - Returns instantly (< 20ms without ML model, < 100ms with)
    - Errors block submission; warnings allow with guidance
    - Persists event to DB asynchronously — zero latency impact
    - Results feed the scoring engine via Redis stream
    """
    start_ms = time.perf_counter()
    redis = await get_redis()

    # ── 1. Idempotency / dedup ─────────────────────────────────────────────
    payload_hash = hashlib.sha256(
        json.dumps(body.payload, sort_keys=True).encode()
    ).hexdigest()[:16]
    dedup_key = f"dedup:{auth.tenant_id}:{body.worker_id}:{payload_hash}"

    if await redis.get(dedup_key):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_submission",
                "message": "Identical payload submitted within the dedup window. Wait 10 minutes or modify the payload.",
            },
        )

    # ── 2. Load worker history from cache / DB ────────────────────────────
    worker_history = await get_worker_history(body.worker_id, auth.tenant_id, db)

    # ── 3. Run rule engine ────────────────────────────────────────────────
    rule_result = rule_engine.run(
        task_type=body.task_type,
        payload=body.payload,
        completion_time=body.completion_time,
        worker_history=worker_history,
    )

    # ── 4. ML hook (non-blocking, runs after rules) ───────────────────────
    quality_score = await ml_quality_hook(
        task_type=body.task_type,
        payload=body.payload,
        base_score=rule_result.quality_score,
        worker_history=worker_history,
    )

    # ── 5. Submit decision ────────────────────────────────────────────────
    has_errors = any(w.severity == "error" for w in rule_result.warnings)
    allow_submit = (not has_errors) and quality_score >= settings.MIN_QUALITY_SCORE_TO_SUBMIT

    # ── 6. Mark dedup key ─────────────────────────────────────────────────
    await redis.setex(dedup_key, settings.DEDUP_WINDOW_SECONDS, "1")

    processed_ms = int((time.perf_counter() - start_ms) * 1000)
    validation_id = f"val_{auth.tenant_id[:8]}_{int(time.time() * 1000)}"

    # ── 7. Persist asynchronously (zero latency impact) ───────────────────
    background_tasks.add_task(
        _persist_validation,
        tenant_id=auth.tenant_id,
        external_worker_id=body.worker_id,
        task_type=body.task_type,
        project_id=body.project_id,
        payload=body.payload,
        completion_time=body.completion_time,
        quality_score=quality_score,
        was_allowed=allow_submit,
        flags=rule_result.flags,
        warning_count=sum(1 for w in rule_result.warnings if w.severity in ("warning", "info")),
        error_count=sum(1 for w in rule_result.warnings if w.severity == "error"),
        processed_ms=processed_ms,
        db=db,
    )

    return ValidateTaskResponse(
        validation_id=validation_id,
        worker_id=body.worker_id,
        task_type=body.task_type,
        quality_score=round(quality_score, 4),
        allow_submit=allow_submit,
        warnings=rule_result.warnings,
        suggestions=rule_result.suggestions,
        flags=rule_result.flags,
        processed_ms=processed_ms,
    )

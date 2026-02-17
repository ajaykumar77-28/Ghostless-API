"""
Ghostless API — Earnings Router

GET /v1/earnings/live               — real-time payout snapshot for a worker
GET /v1/earnings/summary            — daily/weekly/monthly summaries
POST /v1/earnings/task/{task_id}/accept  — mark task as accepted, confirm payout
POST /v1/earnings/task/{task_id}/reject  — mark task as rejected, reverse pending
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, require_auth
from app.models.models import Task, Worker, WorkerScore, EarningsSummary

router = APIRouter(prefix="/earnings", tags=["Earnings"])


# ─── Bonus Tiers (tasks completed today → bonus unlocked) ────────────────────
BONUS_TIERS = [
    {"tasks": 10,  "bonus_usd": 0.50},
    {"tasks": 25,  "bonus_usd": 1.50},
    {"tasks": 50,  "bonus_usd": 4.00},
    {"tasks": 100, "bonus_usd": 10.00},
]


# ─── Response Schemas ─────────────────────────────────────────────────────────

class LiveEarningsResponse(BaseModel):
    worker_id:            str
    session_start:        Optional[str]

    # Time breakdown
    active_minutes:       float
    idle_minutes:         float

    # Money breakdown
    confirmed_usd:        float   # accepted tasks, fully confirmed
    pending_usd:          float   # submitted, awaiting platform review
    session_usd:          float   # earnings this session (with streak applied)
    predicted_payout_usd: float   # confirmed + pending + session

    # Rates
    effective_hourly_usd: float
    streak_multiplier:    float

    # Gamification
    tasks_today:          int
    bonus_progress:       float   # 0.0–1.0 toward next tier
    bonus_threshold_tasks: int
    bonus_amount_usd:     float
    next_bonus_tasks_needed: int
    bonuses_unlocked:     float   # total bonuses earned today

    # Payout info
    next_payout_date:     str
    payout_method:        str


class EarningsSummaryResponse(BaseModel):
    period:         str          # "daily" | "weekly" | "monthly"
    total_usd:      float
    tasks_completed: int
    tasks_accepted: int
    acceptance_rate: float
    bonus_earned:   float
    avg_hourly_usd: float


class TaskStatusUpdate(BaseModel):
    accepted: bool
    payout_override_usd: Optional[float] = None  # override calculated payout


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _calc_streak_multiplier(streak_days: int) -> float:
    days = min(streak_days, settings.MAX_STREAK_BONUS_DAYS)
    return 1.0 + days * settings.STREAK_BONUS_PER_DAY


def _next_payout_date() -> str:
    today = datetime.utcnow()
    days_to_friday = (settings.PAYOUT_DAY_OF_WEEK - today.weekday() + 7) % 7
    if days_to_friday == 0:
        days_to_friday = 7
    payout = today + timedelta(days=days_to_friday)
    return payout.strftime("%Y-%m-%d")


def _calc_bonus_progress(tasks_today: int) -> dict:
    next_tier = next((t for t in BONUS_TIERS if t["tasks"] > tasks_today), BONUS_TIERS[-1])
    progress = min(tasks_today / next_tier["tasks"], 1.0) if next_tier["tasks"] > 0 else 1.0
    tasks_needed = max(0, next_tier["tasks"] - tasks_today)
    return {
        "progress": round(progress, 4),
        "threshold_tasks": next_tier["tasks"],
        "bonus_amount": next_tier["bonus_usd"],
        "tasks_needed": tasks_needed,
    }


def _calc_unlocked_bonuses(tasks_today: int) -> float:
    total = sum(t["bonus_usd"] for t in BONUS_TIERS if tasks_today >= t["tasks"])
    return round(total, 2)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/live", response_model=LiveEarningsResponse, summary="Real-time earnings snapshot")
async def get_live_earnings(
    worker_id: str = Query(..., description="Your platform's worker ID"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a live earnings snapshot including active minutes, predicted payout,
    effective hourly rate, streak multiplier, and bonus progress.

    Poll every 5–10 seconds on the worker-facing UI for a live earnings counter.
    Redis cache with 5-second TTL prevents DB hammering.
    """
    # ── Resolve worker ────────────────────────────────────────────────────
    result = await db.execute(
        select(Worker, WorkerScore)
        .outerjoin(WorkerScore, WorkerScore.worker_id == Worker.id)
        .where(Worker.external_id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(404, detail={"error": "worker_not_found", "worker_id": worker_id})

    worker, score = row
    streak_days = score.streak_days if score else 0

    # ── Today's tasks ─────────────────────────────────────────────────────
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    tasks_result = await db.execute(
        select(Task)
        .where(Task.worker_id == worker.id)
        .where(Task.submitted_at >= today_start)
        .order_by(Task.submitted_at.asc())
    )
    today_tasks = tasks_result.scalars().all()

    # ── All-time totals ───────────────────────────────────────────────────
    confirmed_result = await db.execute(
        select(func.coalesce(func.sum(Task.payout_amount), 0))
        .where(Task.worker_id == worker.id)
        .where(Task.was_accepted == True)
    )
    confirmed_usd = float(confirmed_result.scalar() or 0)

    pending_result = await db.execute(
        select(func.coalesce(func.sum(Task.payout_amount), 0))
        .where(Task.worker_id == worker.id)
        .where(Task.was_accepted == None)
    )
    pending_usd = float(pending_result.scalar() or 0)

    # ── Session calculations ──────────────────────────────────────────────
    session_start = today_tasks[0].submitted_at if today_tasks else None

    active_seconds = sum(t.completion_time or 60 for t in today_tasks)
    active_minutes = round(active_seconds / 60, 1)

    if session_start:
        elapsed_seconds = (datetime.utcnow() - session_start).total_seconds()
        idle_minutes = round(max(0, elapsed_seconds / 60 - active_minutes), 1)
    else:
        idle_minutes = 0.0

    # Session earnings with streak multiplier
    streak_mult = _calc_streak_multiplier(streak_days)
    session_raw = sum(float(t.payout_amount or 0) for t in today_tasks)
    session_usd = round(session_raw * streak_mult, 4)

    predicted_payout = round(confirmed_usd + pending_usd + session_usd, 4)

    # Effective hourly (active time only)
    effective_hourly = round(
        (session_usd / active_minutes * 60) if active_minutes > 0 else 0.0, 2
    )

    # Gamification
    tasks_today = len(today_tasks)
    bonus_info = _calc_bonus_progress(tasks_today)
    bonuses_unlocked = _calc_unlocked_bonuses(tasks_today)

    return LiveEarningsResponse(
        worker_id=worker_id,
        session_start=session_start.isoformat() if session_start else None,
        active_minutes=active_minutes,
        idle_minutes=idle_minutes,
        confirmed_usd=confirmed_usd,
        pending_usd=pending_usd,
        session_usd=session_usd,
        predicted_payout_usd=predicted_payout,
        effective_hourly_usd=effective_hourly,
        streak_multiplier=round(streak_mult, 4),
        tasks_today=tasks_today,
        bonus_progress=bonus_info["progress"],
        bonus_threshold_tasks=bonus_info["threshold_tasks"],
        bonus_amount_usd=bonus_info["bonus_amount"],
        next_bonus_tasks_needed=bonus_info["tasks_needed"],
        bonuses_unlocked=bonuses_unlocked,
        next_payout_date=_next_payout_date(),
        payout_method=worker.metadata_.get("payout_method", "paypal") if worker.metadata_ else "paypal",
    )


@router.get("/summary", response_model=EarningsSummaryResponse, summary="Earnings summary by period")
async def get_earnings_summary(
    worker_id: str = Query(...),
    period: str = Query("weekly", pattern="^(daily|weekly|monthly)$"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Returns aggregated earnings for the given period."""
    result = await db.execute(
        select(Worker).where(Worker.external_id == worker_id).where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    periods = {"daily": 1, "weekly": 7, "monthly": 30}
    since = datetime.utcnow() - timedelta(days=periods[period])

    tasks_result = await db.execute(
        select(Task).where(Task.worker_id == worker.id).where(Task.submitted_at >= since)
    )
    tasks = tasks_result.scalars().all()

    total = sum(float(t.payout_amount or 0) for t in tasks if t.was_accepted)
    accepted = [t for t in tasks if t.was_accepted is True]
    acceptance_rate = len(accepted) / len(tasks) if tasks else 0

    total_active_min = sum((t.completion_time or 60) for t in tasks) / 60
    avg_hourly = round((total / total_active_min * 60) if total_active_min > 0 else 0, 2)

    return EarningsSummaryResponse(
        period=period,
        total_usd=round(total, 4),
        tasks_completed=len(tasks),
        tasks_accepted=len(accepted),
        acceptance_rate=round(acceptance_rate, 4),
        bonus_earned=_calc_unlocked_bonuses(len(tasks)),
        avg_hourly_usd=avg_hourly,
    )


@router.post("/task/{task_id}/accept", summary="Accept a task and confirm payout")
async def accept_task(
    task_id: str,
    body: TaskStatusUpdate,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the client platform after reviewing a submitted task.
    Marks it as accepted and locks in the payout amount.
    Triggers the worker.task_accepted webhook event.
    """
    from sqlalchemy import update as sql_update
    import uuid

    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(400, detail={"error": "invalid_task_id"})

    result = await db.execute(
        select(Task).where(Task.id == task_uuid).where(Task.tenant_id == auth.tenant_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, detail={"error": "task_not_found"})
    if task.was_accepted is not None:
        raise HTTPException(409, detail={"error": "task_already_reviewed", "status": str(task.was_accepted)})

    payout = Decimal(str(body.payout_override_usd)) if body.payout_override_usd else task.payout_amount
    await db.execute(
        sql_update(Task)
        .where(Task.id == task_uuid)
        .values(was_accepted=body.accepted, payout_amount=payout if body.accepted else Decimal("0"))
    )
    await db.commit()

    # Fire webhook (async via Celery)
    from app.tasks.webhooks import dispatch_webhook
    event = "task.accepted" if body.accepted else "task.rejected"
    dispatch_webhook.delay(auth.tenant_id, event, {
        "task_id": task_id,
        "worker_external_id": None,  # enriched in the Celery task from DB
        "payout_usd": float(payout) if body.accepted else 0,
    })

    return {
        "task_id": task_id,
        "accepted": body.accepted,
        "payout_usd": float(payout) if body.accepted else 0,
    }

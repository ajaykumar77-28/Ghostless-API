"""
Ghostless API — Scoring Router

GET  /v1/workers/{worker_id}/score       — fetch a worker's current trust score
GET  /v1/workers/leaderboard             — top workers for a tenant
POST /v1/workers/{worker_id}/score/recalc — force recalculate (async via Celery)
GET  /v1/workers/{worker_id}/promotions  — promotion history
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, require_auth
from app.models.models import Worker, WorkerScore, Promotion

router = APIRouter(prefix="/workers", tags=["Scoring"])


# ─── Response Schemas ─────────────────────────────────────────────────────────

class WorkerScoreResponse(BaseModel):
    worker_id:          str
    external_id:        str
    tier:               str
    trust_score:        float
    accuracy_7d:        Optional[float]
    accuracy_30d:       Optional[float]
    accuracy_all:       Optional[float]
    avg_quality_score:  Optional[float]
    total_tasks:        int
    accepted_tasks:     int
    speed_flag_count:   int
    streak_days:        int
    last_calculated_at: Optional[str]


class LeaderboardEntry(BaseModel):
    rank:           int
    external_id:    str
    tier:           str
    trust_score:    float
    total_tasks:    int
    accuracy_30d:   Optional[float]
    streak_days:    int


class PromotionRecord(BaseModel):
    from_tier:      Optional[str]
    to_tier:        Optional[str]
    reason:         Optional[str]
    trust_score_at: Optional[float]
    triggered_at:   str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/{worker_id}/score", response_model=WorkerScoreResponse, summary="Get worker trust score")
async def get_worker_score(
    worker_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the current trust score and accuracy metrics for a worker.
    Scores are recalculated every 15 minutes by the Celery scoring task.
    """
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

    return WorkerScoreResponse(
        worker_id=str(worker.id),
        external_id=worker.external_id,
        tier=worker.tier.value,
        trust_score=score.trust_score if score else 50.0,
        accuracy_7d=score.accuracy_7d if score else None,
        accuracy_30d=score.accuracy_30d if score else None,
        accuracy_all=score.accuracy_all if score else None,
        avg_quality_score=score.avg_quality_score if score else None,
        total_tasks=score.total_tasks if score else 0,
        accepted_tasks=score.accepted_tasks if score else 0,
        speed_flag_count=score.speed_flag_count if score else 0,
        streak_days=score.streak_days if score else 0,
        last_calculated_at=score.last_calculated_at.isoformat() if (score and score.last_calculated_at) else None,
    )


@router.get("/leaderboard", response_model=List[LeaderboardEntry], summary="Top workers leaderboard")
async def get_leaderboard(
    limit: int = Query(50, ge=1, le=200, description="Max workers to return"),
    tier:  Optional[str] = Query(None, description="Filter by tier: bronze|silver|gold|elite"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the top workers for this tenant, sorted by trust score.
    Use for displaying public leaderboards on your platform.
    """
    query = (
        select(Worker, WorkerScore)
        .join(WorkerScore, WorkerScore.worker_id == Worker.id)
        .where(Worker.tenant_id == auth.tenant_id)
        .where(Worker.status == "active")
        .order_by(desc(WorkerScore.trust_score))
        .limit(limit)
    )
    if tier:
        query = query.where(Worker.tier == tier)

    result = await db.execute(query)
    rows = result.all()

    return [
        LeaderboardEntry(
            rank=i + 1,
            external_id=worker.external_id,
            tier=worker.tier.value,
            trust_score=score.trust_score,
            total_tasks=score.total_tasks,
            accuracy_30d=score.accuracy_30d,
            streak_days=score.streak_days,
        )
        for i, (worker, score) in enumerate(rows)
    ]


@router.post("/{worker_id}/score/recalc", summary="Trigger score recalculation (async)")
async def recalc_score(
    worker_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueues an immediate score recalculation for a specific worker.
    Useful after bulk task acceptance/rejection. Returns immediately.
    """
    # Verify worker exists in this tenant
    result = await db.execute(
        select(Worker)
        .where(Worker.external_id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    # Push to Celery
    from app.tasks.scoring import recalculate_worker_score
    recalculate_worker_score.delay(str(worker.id), auth.tenant_id)

    return {"queued": True, "worker_id": worker_id, "message": "Score recalculation queued. Expect update within 30 seconds."}


@router.get("/{worker_id}/promotions", response_model=List[PromotionRecord], summary="Worker promotion history")
async def get_promotions(
    worker_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the full promotion/demotion history for a worker.
    Show this on the worker's profile to build trust and motivation.
    """
    result = await db.execute(
        select(Worker).where(Worker.external_id == worker_id).where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    promo_result = await db.execute(
        select(Promotion)
        .where(Promotion.worker_id == worker.id)
        .order_by(desc(Promotion.triggered_at))
        .limit(20)
    )
    promotions = promo_result.scalars().all()

    return [
        PromotionRecord(
            from_tier=p.from_tier.value if p.from_tier else None,
            to_tier=p.to_tier.value if p.to_tier else None,
            reason=p.reason,
            trust_score_at=p.trust_score_at,
            triggered_at=p.triggered_at.isoformat(),
        )
        for p in promotions
    ]

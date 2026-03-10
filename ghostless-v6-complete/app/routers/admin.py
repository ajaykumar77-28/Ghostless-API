"""
Ghostless API — Admin Router (v5)

New in v5:
  - Scoring decision explainability endpoint
  - Fraud decision explainability (reason codes + decay breakdown)
  - Baseline health metrics
  - Worker lifecycle distribution
  - Anomaly rate dashboard
  - Coordinated attack timeline view
  - Admin bulk actions (suspend/review/shadow_ban)
  - Worker appeal workflow
  - Admin action audit logging on every mutating endpoint
  - Per-tenant config hot-update
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, text, and_, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, require_auth
from app.models.models import (
    FraudEvent, Worker, WorkerScore, LedgerEntry, AdminAuditLog,
    AppealRecord, CoordinatedAttackEvent, ScoringDecisionLog,
    BaselineVersion, TenantConfig, AppealStatus,
)

router = APIRouter(prefix="/admin", tags=["Admin"])


# ─── Auth guard ───────────────────────────────────────────────────────────────

def _require_admin(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    if getattr(auth, "role", None) != "admin":
        raise HTTPException(403, detail={"error": "admin_required"})
    return auth


# ─── Audit log helper ─────────────────────────────────────────────────────────

async def _audit(
    db: AsyncSession,
    auth: AuthContext,
    action: str,
    target_type: str,
    target_id: str,
    before: dict = None,
    after: dict = None,
    notes: str = "",
):
    log = AdminAuditLog(
        tenant_id    = auth.tenant_id,
        admin_key_id = getattr(auth, "api_key_id", None),
        action       = action,
        target_type  = target_type,
        target_id    = target_id,
        before_state = before or {},
        after_state  = after or {},
        notes        = notes,
    )
    db.add(log)


# ─── Response Schemas ─────────────────────────────────────────────────────────

class FraudMetrics(BaseModel):
    window_days:       int
    total_events:      int
    critical_events:   int
    warning_events:    int
    unique_workers:    int
    fraud_rate_pct:    float
    top_event_types:   List[dict]
    auto_suspended:    int
    coordinated_attacks: int
    shadow_banned_workers: int


class TrustDistribution(BaseModel):
    bucket_0_20:   int
    bucket_20_40:  int
    bucket_40_60:  int
    bucket_60_80:  int
    bucket_80_100: int
    mean_trust:    float
    p25_trust:     float
    p50_trust:     float
    p75_trust:     float
    workers_at_ceiling:     int
    lifecycle_distribution: dict


class BaselineHealthReport(BaseModel):
    task_type:         str
    total_observations: int
    current_mean:      float
    current_std:       float
    drift_detected:    bool
    drift_magnitude:   float
    last_snapshot_at:  Optional[str]
    version:           int


class WorkerLifecycleMetrics(BaseModel):
    new_count:       int
    learning_count:  int
    trusted_count:   int
    flagged_count:   int
    banned_count:    int
    shadow_banned_count: int


class ScoringExplanation(BaseModel):
    worker_id:          str
    trust_score_before: float
    trust_score_after:  float
    ewma_component:     float
    accuracy_component: float
    streak_component:   float
    tenure_component:   float
    confidence_factor:  float
    fraud_multiplier:   float
    velocity_penalty:   float
    task_difficulty:    float
    fraud_event_count:  int
    drift_detected:     bool
    drift_details:      dict
    calculated_at:      str


class FraudExplanation(BaseModel):
    worker_id:           str
    external_id:         str
    fraud_events:        List[dict]
    total_events:        int
    unreviewed_events:   int
    effective_weight:    float   # sum of decayed weights
    fraud_multiplier:    float
    reason_codes:        List[str]
    progressive_level:   int


# ─── Fraud Metrics ────────────────────────────────────────────────────────────

@router.get("/metrics/fraud", response_model=FraudMetrics)
async def get_fraud_metrics(
    window_days: int = Query(7, ge=1, le=90),
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=window_days)

    result = await db.execute(
        select(
            func.count(FraudEvent.id).label("total"),
            func.count(FraudEvent.id).filter(FraudEvent.severity == "critical").label("critical"),
            func.count(FraudEvent.id).filter(FraudEvent.severity == "warning").label("warning"),
            func.count(func.distinct(FraudEvent.worker_id)).label("unique_workers"),
            func.count(FraudEvent.id).filter(FraudEvent.auto_action == "suspend").label("suspended"),
        )
        .where(FraudEvent.tenant_id == auth.tenant_id)
        .where(FraudEvent.created_at >= since)
    )
    row       = result.first()
    total, critical, warning, unique_workers, auto_suspended = row

    active_result = await db.execute(
        select(func.count(Worker.id))
        .where(Worker.tenant_id == auth.tenant_id)
        .where(Worker.status == "active")
    )
    total_active = active_result.scalar() or 1

    # Coordinated attacks in window
    coord_result = await db.execute(
        select(func.count(CoordinatedAttackEvent.id))
        .where(CoordinatedAttackEvent.tenant_id == auth.tenant_id)
        .where(CoordinatedAttackEvent.created_at >= since)
    )
    coord_count = coord_result.scalar() or 0

    # Shadow-banned count
    shadow_result = await db.execute(
        select(func.count(WorkerScore.worker_id))
        .join(Worker, Worker.id == WorkerScore.worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
        .where(WorkerScore.shadow_banned == True)
    )
    shadow_count = shadow_result.scalar() or 0

    type_result = await db.execute(
        select(FraudEvent.event_type, func.count(FraudEvent.id).label("n"))
        .where(FraudEvent.tenant_id == auth.tenant_id)
        .where(FraudEvent.created_at >= since)
        .group_by(FraudEvent.event_type)
        .order_by(text("n DESC"))
        .limit(10)
    )
    top_types = [{"event_type": r[0], "count": r[1]} for r in type_result.all()]

    return FraudMetrics(
        window_days=window_days,
        total_events=total or 0,
        critical_events=critical or 0,
        warning_events=warning or 0,
        unique_workers=unique_workers or 0,
        fraud_rate_pct=round((unique_workers or 0) / total_active * 100, 2),
        top_event_types=top_types,
        auto_suspended=auto_suspended or 0,
        coordinated_attacks=coord_count,
        shadow_banned_workers=shadow_count,
    )


# ─── Trust Distribution ───────────────────────────────────────────────────────

@router.get("/metrics/trust", response_model=TrustDistribution)
async def get_trust_distribution(
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            func.count(WorkerScore.worker_id).filter(WorkerScore.trust_score < 20).label("b0"),
            func.count(WorkerScore.worker_id).filter(
                and_(WorkerScore.trust_score >= 20, WorkerScore.trust_score < 40)).label("b20"),
            func.count(WorkerScore.worker_id).filter(
                and_(WorkerScore.trust_score >= 40, WorkerScore.trust_score < 60)).label("b40"),
            func.count(WorkerScore.worker_id).filter(
                and_(WorkerScore.trust_score >= 60, WorkerScore.trust_score < 80)).label("b60"),
            func.count(WorkerScore.worker_id).filter(WorkerScore.trust_score >= 80).label("b80"),
            func.avg(WorkerScore.trust_score).label("mean"),
            func.percentile_cont(0.25).within_group(WorkerScore.trust_score).label("p25"),
            func.percentile_cont(0.50).within_group(WorkerScore.trust_score).label("p50"),
            func.percentile_cont(0.75).within_group(WorkerScore.trust_score).label("p75"),
            func.count(WorkerScore.worker_id).filter(WorkerScore.max_trust < 100).label("at_ceiling"),
        )
        .where(WorkerScore.tenant_id == auth.tenant_id)
    )
    row = result.first()
    b0, b20, b40, b60, b80, mean, p25, p50, p75, at_ceiling = row

    # Lifecycle distribution
    lc_result = await db.execute(
        select(WorkerScore.lifecycle_stage, func.count(WorkerScore.worker_id).label("n"))
        .where(WorkerScore.tenant_id == auth.tenant_id)
        .group_by(WorkerScore.lifecycle_stage)
    )
    lifecycle_dist = {str(r[0]): r[1] for r in lc_result.all()}

    return TrustDistribution(
        bucket_0_20=b0 or 0, bucket_20_40=b20 or 0,
        bucket_40_60=b40 or 0, bucket_60_80=b60 or 0, bucket_80_100=b80 or 0,
        mean_trust=round(float(mean or 0), 2),
        p25_trust=round(float(p25 or 0), 2),
        p50_trust=round(float(p50 or 0), 2),
        p75_trust=round(float(p75 or 0), 2),
        workers_at_ceiling=at_ceiling or 0,
        lifecycle_distribution=lifecycle_dist,
    )


# ─── Baseline Health ──────────────────────────────────────────────────────────

@router.get("/metrics/baselines", response_model=List[BaselineHealthReport])
async def get_baseline_health(
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Per-task-type baseline health — drift, std, observation counts."""
    import math

    result = await db.execute(
        select(WorkerScore.task_baselines)
        .where(WorkerScore.tenant_id == auth.tenant_id)
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if not row:
        return []

    baselines = row or {}
    reports = []
    for task_type, bl in baselines.items():
        n    = bl.get("n", 0)
        mean = bl.get("mean", 0)
        m2   = bl.get("m2", 0)
        std  = math.sqrt(m2 / max(n, 1)) if n >= 2 else 0.0

        # Get latest baseline snapshot for drift comparison
        snap_result = await db.execute(
            select(BaselineVersion)
            .where(BaselineVersion.tenant_id == auth.tenant_id)
            .where(BaselineVersion.task_type == task_type)
            .order_by(BaselineVersion.version.desc())
            .limit(1)
        )
        snap = snap_result.scalar_one_or_none()

        drift_mag   = 0.0
        drift_found = False
        if snap and std > 0:
            snap_mean = snap.baseline.get("mean", mean)
            snap_std  = std  # use current std for comparison
            delta     = abs(mean - snap_mean)
            drift_mag = round(delta / max(snap_std, 1e-6), 3)
            drift_found = drift_mag > 2.0

        reports.append(BaselineHealthReport(
            task_type=task_type,
            total_observations=n,
            current_mean=round(mean, 4),
            current_std=round(std, 4),
            drift_detected=drift_found,
            drift_magnitude=drift_mag,
            last_snapshot_at=snap.created_at.isoformat() if snap else None,
            version=snap.version if snap else 0,
        ))

    return reports


# ─── Worker Lifecycle Metrics ─────────────────────────────────────────────────

@router.get("/metrics/lifecycle", response_model=WorkerLifecycleMetrics)
async def get_lifecycle_metrics(
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Worker lifecycle stage distribution + shadow-ban count."""
    result = await db.execute(
        select(
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.lifecycle_stage == "new").label("new_c"),
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.lifecycle_stage == "learning").label("learning_c"),
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.lifecycle_stage == "trusted").label("trusted_c"),
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.lifecycle_stage == "flagged").label("flagged_c"),
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.lifecycle_stage == "banned").label("banned_c"),
            func.count(WorkerScore.worker_id).filter(
                WorkerScore.shadow_banned == True).label("shadow_c"),
        )
        .where(WorkerScore.tenant_id == auth.tenant_id)
    )
    row = result.first()
    return WorkerLifecycleMetrics(
        new_count=row[0] or 0, learning_count=row[1] or 0,
        trusted_count=row[2] or 0, flagged_count=row[3] or 0,
        banned_count=row[4] or 0, shadow_banned_count=row[5] or 0,
    )


# ─── Coordinated Attack Timeline ──────────────────────────────────────────────

@router.get("/metrics/coordinated-attacks")
async def get_coordinated_attack_timeline(
    window_days: int = Query(7, ge=1, le=30),
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Timeline of coordinated attack events."""
    since = datetime.utcnow() - timedelta(days=window_days)
    result = await db.execute(
        select(CoordinatedAttackEvent)
        .where(CoordinatedAttackEvent.tenant_id == auth.tenant_id)
        .where(CoordinatedAttackEvent.created_at >= since)
        .order_by(CoordinatedAttackEvent.created_at.desc())
        .limit(100)
    )
    events = result.scalars().all()
    return [
        {
            "id":             str(e.id),
            "worker_count":   e.worker_count,
            "detection_type": e.detection_type,
            "lookback_hours": e.lookback_hours,
            "reviewed":       e.reviewed,
            "created_at":     e.created_at.isoformat(),
        }
        for e in events
    ]


# ─── Scoring Explainability ───────────────────────────────────────────────────

@router.get("/workers/{worker_id}/scoring-history",
            response_model=List[ScoringExplanation])
async def get_scoring_history(
    worker_id: str,
    limit: int = Query(20, ge=1, le=100),
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Full scoring decision log for a worker — why trust changed."""
    result = await db.execute(
        select(Worker)
        .where(Worker.id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    log_result = await db.execute(
        select(ScoringDecisionLog)
        .where(ScoringDecisionLog.worker_id == worker_id)
        .where(ScoringDecisionLog.tenant_id == auth.tenant_id)
        .order_by(ScoringDecisionLog.calculated_at.desc())
        .limit(limit)
    )
    logs = log_result.scalars().all()

    return [
        ScoringExplanation(
            worker_id=str(l.worker_id),
            trust_score_before=l.trust_score_before or 0,
            trust_score_after=l.trust_score_after or 0,
            ewma_component=l.ewma_component or 0,
            accuracy_component=l.accuracy_component or 0,
            streak_component=l.streak_component or 0,
            tenure_component=l.tenure_component or 0,
            confidence_factor=l.confidence_factor or 0,
            fraud_multiplier=l.fraud_multiplier or 1.0,
            velocity_penalty=l.velocity_penalty or 0,
            task_difficulty=l.task_difficulty_used or 1.0,
            fraud_event_count=l.fraud_event_count or 0,
            drift_detected=l.drift_detected or False,
            drift_details=l.drift_details or {},
            calculated_at=l.calculated_at.isoformat(),
        )
        for l in logs
    ]


# ─── Fraud Explainability ─────────────────────────────────────────────────────

@router.get("/workers/{worker_id}/fraud-explanation",
            response_model=FraudExplanation)
async def get_fraud_explanation(
    worker_id: str,
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Explains why a worker's fraud score is what it is, with decay breakdown."""
    import math

    result = await db.execute(
        select(Worker)
        .where(Worker.id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    fe_result = await db.execute(
        select(FraudEvent)
        .where(FraudEvent.worker_id == worker_id)
        .where(FraudEvent.tenant_id == auth.tenant_id)
        .order_by(FraudEvent.created_at.desc())
        .limit(50)
    )
    events = fe_result.scalars().all()

    now = datetime.utcnow()
    HALFLIFE = 90.0
    all_reason_codes = set()
    max_progressive  = 0
    total_weight     = 0.0

    event_details = []
    for e in events:
        age_days = (now - e.created_at).total_seconds() / 86400
        weight   = 2 ** (-age_days / HALFLIFE)
        if not e.reviewed:
            total_weight += weight
        for rc in (e.reason_codes or []):
            all_reason_codes.add(rc)
        if e.progressive_level and e.progressive_level > max_progressive:
            max_progressive = e.progressive_level
        event_details.append({
            "id":         str(e.id),
            "event_type": e.event_type,
            "severity":   e.severity,
            "reason_codes": e.reason_codes or [],
            "age_days":   round(age_days, 1),
            "weight":     round(weight, 4),
            "reviewed":   e.reviewed,
            "progressive_level": e.progressive_level,
            "created_at": e.created_at.isoformat(),
        })

    capped_weight = min(total_weight, 3.0)
    fraud_mult    = round(0.5 ** capped_weight, 4)

    return FraudExplanation(
        worker_id=str(worker.id),
        external_id=worker.external_id,
        fraud_events=event_details,
        total_events=len(events),
        unreviewed_events=sum(1 for e in events if not e.reviewed),
        effective_weight=round(total_weight, 4),
        fraud_multiplier=fraud_mult,
        reason_codes=list(all_reason_codes),
        progressive_level=max_progressive,
    )


# ─── Bulk Actions ─────────────────────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    worker_ids: List[str]
    action:     str   # suspend | unsuspend | shadow_ban | review_fraud
    notes:      Optional[str] = None


@router.post("/workers/bulk-action")
async def bulk_worker_action(
    body: BulkActionRequest,
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Bulk admin actions on multiple workers at once."""
    VALID_ACTIONS = {"suspend", "unsuspend", "shadow_ban", "review_fraud"}
    if body.action not in VALID_ACTIONS:
        raise HTTPException(400, detail={"error": "invalid_action",
                                         "valid": list(VALID_ACTIONS)})

    results = {"success": [], "not_found": []}

    for worker_id in body.worker_ids:
        result = await db.execute(
            select(Worker)
            .where(Worker.id == worker_id)
            .where(Worker.tenant_id == auth.tenant_id)
        )
        worker = result.scalar_one_or_none()
        if not worker:
            results["not_found"].append(worker_id)
            continue

        before = {"status": worker.status.value}

        if body.action == "suspend":
            await db.execute(
                sql_update(Worker).where(Worker.id == worker_id).values(status="suspended")
            )
        elif body.action == "unsuspend":
            await db.execute(
                sql_update(Worker).where(Worker.id == worker_id).values(status="active")
            )
        elif body.action == "shadow_ban":
            await db.execute(
                sql_update(WorkerScore)
                .where(WorkerScore.worker_id == worker_id)
                .values(shadow_banned=True)
            )
        elif body.action == "review_fraud":
            await db.execute(
                sql_update(FraudEvent)
                .where(FraudEvent.worker_id == worker_id)
                .where(FraudEvent.tenant_id == auth.tenant_id)
                .where(FraudEvent.reviewed == False)
                .values(reviewed=True, reviewed_by="bulk_admin_action")
            )
            await db.execute(
                sql_update(WorkerScore)
                .where(WorkerScore.worker_id == worker_id)
                .values(max_trust=100.0)
            )

        await _audit(db, auth, f"bulk_{body.action}", "worker", worker_id,
                     before=before, notes=body.notes or "")
        results["success"].append(worker_id)

    await db.commit()
    return {"action": body.action, "results": results}


# ─── Single Worker Review ─────────────────────────────────────────────────────

@router.post("/workers/{worker_id}/review")
async def review_worker(
    worker_id: str,
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Clear fraud events, restore max_trust. Logs admin action."""
    result = await db.execute(
        select(Worker, WorkerScore)
        .outerjoin(WorkerScore, WorkerScore.worker_id == Worker.id)
        .where(Worker.id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(404, detail={"error": "worker_not_found"})
    worker, score = row

    before = {
        "max_trust":     score.max_trust    if score else 100.0,
        "fraud_flags":   score.fraud_flag_count if score else 0,
        "shadow_banned": score.shadow_banned if score else False,
    }

    await db.execute(
        sql_update(FraudEvent)
        .where(FraudEvent.worker_id == worker_id)
        .where(FraudEvent.tenant_id == auth.tenant_id)
        .where(FraudEvent.reviewed == False)
        .values(reviewed=True, reviewed_by="admin_review")
    )
    await db.execute(
        sql_update(WorkerScore)
        .where(WorkerScore.worker_id == worker_id)
        .values(max_trust=100.0, shadow_banned=False)
    )

    await _audit(db, auth, "review_worker", "worker", worker_id,
                 before=before, after={"max_trust": 100.0, "shadow_banned": False})
    await db.commit()

    return {"worker_id": worker_id, "reviewed": True, "max_trust_restored": 100.0}


# ─── Appeals ─────────────────────────────────────────────────────────────────

class AppealCreateRequest(BaseModel):
    worker_id:       str
    fraud_event_id:  Optional[str] = None
    reason:          str
    evidence:        Optional[dict] = None


class AppealResolveRequest(BaseModel):
    status:         str   # approved | denied
    reviewer_notes: Optional[str] = None


@router.post("/appeals", summary="Worker submits an appeal")
async def create_appeal(
    body: AppealCreateRequest,
    auth: AuthContext = Depends(require_auth),   # workers can create appeals
    db: AsyncSession = Depends(get_db),
):
    """V5: Worker appeal workflow entry point."""
    result = await db.execute(
        select(Worker)
        .where(Worker.id == body.worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    # Check for duplicate pending appeal
    existing = await db.execute(
        select(AppealRecord)
        .where(AppealRecord.worker_id == body.worker_id)
        .where(AppealRecord.status == AppealStatus.pending)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail={"error": "appeal_already_pending"})

    appeal = AppealRecord(
        tenant_id      = auth.tenant_id,
        worker_id      = body.worker_id,
        fraud_event_id = body.fraud_event_id,
        reason         = body.reason,
        evidence       = body.evidence or {},
        status         = AppealStatus.pending,
    )
    db.add(appeal)
    await db.commit()
    await db.refresh(appeal)

    return {"appeal_id": str(appeal.id), "status": appeal.status.value}


@router.patch("/appeals/{appeal_id}", summary="Admin resolves an appeal")
async def resolve_appeal(
    appeal_id: str,
    body: AppealResolveRequest,
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Admin approves or denies an appeal. On approval, clears fraud and restores trust."""
    result = await db.execute(
        select(AppealRecord)
        .where(AppealRecord.id == appeal_id)
        .where(AppealRecord.tenant_id == auth.tenant_id)
    )
    appeal = result.scalar_one_or_none()
    if not appeal:
        raise HTTPException(404, detail={"error": "appeal_not_found"})

    new_status = AppealStatus.approved if body.status == "approved" else AppealStatus.denied

    await db.execute(
        sql_update(AppealRecord)
        .where(AppealRecord.id == appeal_id)
        .values(
            status=new_status,
            reviewer_notes=body.reviewer_notes,
            reviewed_by=getattr(auth, "tenant_id", "admin"),
            resolved_at=datetime.utcnow(),
        )
    )

    if new_status == AppealStatus.approved:
        # Clear fraud events and restore trust
        await db.execute(
            sql_update(FraudEvent)
            .where(FraudEvent.worker_id == appeal.worker_id)
            .where(FraudEvent.tenant_id == auth.tenant_id)
            .where(FraudEvent.reviewed == False)
            .values(reviewed=True, reviewed_by=f"appeal_{appeal_id}")
        )
        await db.execute(
            sql_update(WorkerScore)
            .where(WorkerScore.worker_id == appeal.worker_id)
            .values(max_trust=100.0, shadow_banned=False)
        )

    await _audit(db, auth, f"appeal_{body.status}", "appeal", appeal_id,
                 after={"status": body.status, "notes": body.reviewer_notes})
    await db.commit()

    return {"appeal_id": appeal_id, "resolved": True, "status": body.status}


# ─── Tenant Config Hot-Update ─────────────────────────────────────────────────

class TenantConfigUpdate(BaseModel):
    max_tasks_per_hour:          Optional[int]   = None
    velocity_trust_penalty_max:  Optional[float] = None
    min_peers_for_confidence_50: Optional[int]   = None
    post_fraud_max_trust:        Optional[float] = None
    fraud_progressive_step1:     Optional[int]   = None
    fraud_progressive_step2:     Optional[int]   = None
    fraud_progressive_step3:     Optional[int]   = None
    default_currency:            Optional[str]   = None


@router.patch("/config", summary="Update per-tenant scoring config (hot reload)")
async def update_tenant_config(
    body: TenantConfigUpdate,
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """V5: Hot-update per-tenant scoring parameters. No restart required."""
    result = await db.execute(
        select(TenantConfig).where(TenantConfig.tenant_id == auth.tenant_id)
    )
    cfg = result.scalar_one_or_none()

    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, detail={"error": "no_fields_to_update"})

    before = {}
    if cfg:
        before = {k: getattr(cfg, k) for k in updates if hasattr(cfg, k)}
        await db.execute(
            sql_update(TenantConfig)
            .where(TenantConfig.tenant_id == auth.tenant_id)
            .values(**updates)
        )
    else:
        new_cfg = TenantConfig(tenant_id=auth.tenant_id, **updates)
        db.add(new_cfg)

    await _audit(db, auth, "update_tenant_config", "tenant", str(auth.tenant_id),
                 before=before, after=updates)
    await db.commit()
    return {"updated": True, "fields": list(updates.keys())}


# ─── Payout Leakage ───────────────────────────────────────────────────────────

@router.get("/metrics/payout-leakage")
async def get_payout_leakage(
    window_days: int = Query(30, ge=1, le=90),
    auth: AuthContext = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Payouts issued to fraud-suspected workers."""
    since = datetime.utcnow() - timedelta(days=window_days)

    fraud_workers = await db.execute(
        select(func.distinct(FraudEvent.worker_id))
        .where(FraudEvent.tenant_id == auth.tenant_id)
        .where(FraudEvent.reviewed == False)
    )
    fraud_worker_ids = [r[0] for r in fraud_workers.all()]

    if not fraud_worker_ids:
        return {"total_leakage_usd": 0, "affected_workers": 0, "entries": []}

    payout_result = await db.execute(
        select(
            LedgerEntry.worker_id,
            LedgerEntry.currency,
            func.sum(LedgerEntry.amount_usd).label("total_usd"),
            func.count(LedgerEntry.id).label("entry_count"),
        )
        .where(LedgerEntry.tenant_id == auth.tenant_id)
        .where(LedgerEntry.worker_id.in_(fraud_worker_ids))
        .where(LedgerEntry.is_confirmed == True)
        .where(LedgerEntry.created_at >= since)
        .group_by(LedgerEntry.worker_id, LedgerEntry.currency)
    )
    rows = payout_result.all()

    total_usd = sum(float(r[2] or 0) for r in rows if r[1] == "USD")
    return {
        "total_leakage_usd": round(total_usd, 4),
        "affected_workers":  len({r[0] for r in rows}),
        "entries": [
            {"worker_id": str(r[0]), "currency": r[1],
             "total": float(r[2] or 0), "entry_count": r[3]}
            for r in rows
        ],
    }

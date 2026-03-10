"""
Ghostless API — Score Explanation & Feature Flags Router (v6)

Adds two new endpoints:

  GET  /v1/scores/{worker_id}/explain
       Returns the full explainability breakdown for a worker's current trust score,
       including posterior state, confidence interval, score history trend,
       and a human-readable narrative.

  GET  /v1/scores/{worker_id}/history
       Returns a time-series of trust score changes with per-decision explanations.

  GET/POST /v1/admin/flags
       Manage per-tenant feature flags (blue/green rollout, algorithm toggles).

These endpoints power the tenant-facing dashboard and SDK.
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, require_auth
from app.middleware.rbac import require_permission
from app.models.models import Worker, WorkerScore, ScoringDecisionLog
from app.engine.bayesian import (
    beta_credible_interval,
    beta_mean,
    ALGORITHM_VERSION,
)

router = APIRouter(tags=["Scores & Explanation"])


# ─── Response models ──────────────────────────────────────────────────────────

class CredibleInterval(BaseModel):
    lower: float
    upper: float
    width: float
    confidence_pct: int = 90


class ScoreExplanation(BaseModel):
    worker_id:          str
    trust_score:        float
    algorithm_version:  str
    credible_interval:  CredibleInterval
    posterior_alpha:    float
    posterior_beta:     float
    posterior_mean:     float     # E[p] of Beta distribution
    total_tasks:        int
    lifecycle_stage:    str
    score_volatility:   Optional[float]
    components: Dict[str, Any]   # full breakdown
    narrative:  str              # human-readable summary
    last_updated: Optional[str]


class ScoreHistoryPoint(BaseModel):
    calculated_at:    str
    trust_before:     Optional[float]
    trust_after:      Optional[float]
    delta:            Optional[float]
    fraud_multiplier: Optional[float]
    confidence_factor: Optional[float]
    is_fraud_suspect:  Optional[bool]
    algorithm_version: Optional[str]


class ScoreHistoryResponse(BaseModel):
    worker_id: str
    history:   List[ScoreHistoryPoint]
    total:     int


class FeatureFlag(BaseModel):
    flag_name:   str
    enabled:     bool
    rollout_pct: int = 100
    metadata:    Dict[str, Any] = {}


# ─── Narrative generation ─────────────────────────────────────────────────────

def _build_narrative(
    trust_score:    float,
    posterior_mean: float,
    ci_lower:       float,
    ci_upper:       float,
    total_tasks:    int,
    lifecycle:      str,
    volatility:     Optional[float],
) -> str:
    """Generate a plain-English explanation of the current trust score."""
    tier = (
        "just getting started"    if trust_score < 40 else
        "building a track record" if trust_score < 60 else
        "a reliable contributor"  if trust_score < 75 else
        "highly trusted"          if trust_score < 90 else
        "an elite contributor"
    )

    uncertainty = (
        "The score is still highly uncertain due to limited task history."
        if total_tasks < 15 else
        f"The 90% credible interval is [{ci_lower:.0%}–{ci_upper:.0%}], "
        f"{'indicating broad uncertainty' if (ci_upper - ci_lower) > 0.30 else 'indicating reasonable confidence'}."
    )

    volatility_note = ""
    if volatility is not None and volatility > 5.0:
        volatility_note = " The score has been fluctuating significantly recently."

    return (
        f"This worker is {tier} with a trust score of {trust_score:.1f}/100. "
        f"Based on {total_tasks} scored tasks, our Bayesian model estimates their "
        f"true acceptance probability at {posterior_mean:.1%}. "
        f"{uncertainty}"
        f"{volatility_note}"
    )


# ─── Score explanation endpoint ───────────────────────────────────────────────

@router.get(
    "/scores/{external_worker_id}/explain",
    response_model=ScoreExplanation,
    summary="Get full explanation for a worker's trust score",
)
async def explain_score(
    external_worker_id: str,
    auth:               AuthContext  = Depends(require_auth),
    _perm:              None        = Depends(require_permission("scores:read:any")),
    db:                 AsyncSession = Depends(get_db),
):
    """
    Returns the complete explainability breakdown for a worker's current trust score.

    Includes:
    - Bayesian posterior parameters and credible interval
    - Per-component breakdown (EWMA, accuracy, streak, tenure, fraud, velocity)
    - Human-readable narrative
    - Algorithm version (so you can reproduce historical scores)
    """
    result = await db.execute(
        select(Worker, WorkerScore)
        .outerjoin(WorkerScore, WorkerScore.worker_id == Worker.id)
        .where(Worker.external_id == external_worker_id)
        .where(Worker.tenant_id   == auth.tenant_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error":   "worker_not_found",
            "worker_id": external_worker_id,
        })

    worker, score = row

    # Posterior parameters
    baselines      = (score.task_baselines or {}) if score else {}
    posterior_data = baselines.get("__bayesian__", {})
    alpha = posterior_data.get("alpha", 2.0)
    beta_ = posterior_data.get("beta",  2.0)

    # Credible interval
    ci_lo, ci_hi = beta_credible_interval(alpha, beta_)
    post_mean    = beta_mean(alpha, beta_)

    trust_score  = score.trust_score if score else 50.0
    total_tasks  = score.total_tasks if score else 0
    lifecycle    = score.lifecycle_stage.value if (score and score.lifecycle_stage) else "new"
    volatility   = getattr(score, "score_volatility", None)

    # Components from the most recent scoring decision log
    log_result = await db.execute(
        select(ScoringDecisionLog)
        .where(ScoringDecisionLog.worker_id == worker.id)
        .where(ScoringDecisionLog.tenant_id == auth.tenant_id)
        .order_by(desc(ScoringDecisionLog.calculated_at))
        .limit(1)
    )
    latest_log = log_result.scalar_one_or_none()

    components: Dict[str, Any] = {}
    if latest_log:
        components = {
            "ewma_component":      latest_log.ewma_component,
            "accuracy_component":  latest_log.accuracy_component,
            "streak_component":    latest_log.streak_component,
            "tenure_component":    latest_log.tenure_component,
            "confidence_factor":   latest_log.confidence_factor,
            "fraud_multiplier":    latest_log.fraud_multiplier,
            "velocity_penalty":    latest_log.velocity_penalty,
            "task_difficulty_used": latest_log.task_difficulty_used,
            "is_fraud_suspect":    latest_log.is_fraud_suspect,
            "drift_detected":      latest_log.drift_detected,
        }

    narrative = _build_narrative(
        trust_score    = trust_score,
        posterior_mean = post_mean,
        ci_lower       = ci_lo,
        ci_upper       = ci_hi,
        total_tasks    = total_tasks,
        lifecycle      = lifecycle,
        volatility     = volatility,
    )

    return ScoreExplanation(
        worker_id         = external_worker_id,
        trust_score       = trust_score,
        algorithm_version = ALGORITHM_VERSION,
        credible_interval = CredibleInterval(
            lower = ci_lo, upper = ci_hi,
            width = round(ci_hi - ci_lo, 4),
        ),
        posterior_alpha   = round(alpha, 4),
        posterior_beta    = round(beta_, 4),
        posterior_mean    = round(post_mean, 4),
        total_tasks       = total_tasks,
        lifecycle_stage   = lifecycle,
        score_volatility  = volatility,
        components        = components,
        narrative         = narrative,
        last_updated      = score.last_calculated_at.isoformat() if (score and score.last_calculated_at) else None,
    )


# ─── Score history endpoint ───────────────────────────────────────────────────

@router.get(
    "/scores/{external_worker_id}/history",
    response_model=ScoreHistoryResponse,
    summary="Get trust score history with per-decision explanations",
)
async def score_history(
    external_worker_id: str,
    limit:  int         = Query(default=50, le=200),
    offset: int         = Query(default=0,  ge=0),
    auth:   AuthContext = Depends(require_auth),
    _perm:  None        = Depends(require_permission("scores:read:any")),
    db:     AsyncSession = Depends(get_db),
):
    """
    Returns a paginated time-series of every trust score change for a worker,
    including the breakdown of what drove each change.
    """
    worker_result = await db.execute(
        select(Worker)
        .where(Worker.external_id == external_worker_id)
        .where(Worker.tenant_id   == auth.tenant_id)
    )
    worker = worker_result.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail={"error": "worker_not_found"})

    logs_result = await db.execute(
        select(ScoringDecisionLog)
        .where(ScoringDecisionLog.worker_id == worker.id)
        .where(ScoringDecisionLog.tenant_id == auth.tenant_id)
        .order_by(desc(ScoringDecisionLog.calculated_at))
        .offset(offset)
        .limit(limit)
    )
    logs = logs_result.scalars().all()

    history = [
        ScoreHistoryPoint(
            calculated_at     = log.calculated_at.isoformat(),
            trust_before      = log.trust_score_before,
            trust_after       = log.trust_score_after,
            delta             = round((log.trust_score_after or 0) - (log.trust_score_before or 0), 4)
                                if log.trust_score_before and log.trust_score_after else None,
            fraud_multiplier  = log.fraud_multiplier,
            confidence_factor = log.confidence_factor,
            is_fraud_suspect  = log.is_fraud_suspect,
            algorithm_version = getattr(log, "algorithm_version", None),
        )
        for log in logs
    ]

    return ScoreHistoryResponse(
        worker_id = external_worker_id,
        history   = history,
        total     = len(history),
    )


# ─── Feature flags endpoints ──────────────────────────────────────────────────

@router.get(
    "/admin/flags",
    response_model=List[FeatureFlag],
    summary="List feature flags for this tenant",
)
async def list_flags(
    auth:  AuthContext  = Depends(require_auth),
    _perm: None         = Depends(require_permission("feature_flags:read")),
    db:    AsyncSession = Depends(get_db),
):
    """List all feature flags for the current tenant (plus global flags)."""
    from sqlalchemy import text
    result = await db.execute(
        text("""
            SELECT flag_name, enabled, rollout_pct, metadata
            FROM feature_flags
            WHERE tenant_id = :tenant_id OR tenant_id IS NULL
            ORDER BY flag_name
        """),
        {"tenant_id": auth.tenant_id},
    )
    rows = result.all()
    return [
        FeatureFlag(
            flag_name   = row[0],
            enabled     = row[1],
            rollout_pct = row[2] or 100,
            metadata    = row[3] or {},
        )
        for row in rows
    ]


@router.post(
    "/admin/flags",
    response_model=FeatureFlag,
    summary="Create or update a feature flag",
)
async def upsert_flag(
    flag:  FeatureFlag,
    auth:  AuthContext  = Depends(require_auth),
    _perm: None         = Depends(require_permission("feature_flags:write")),
    db:    AsyncSession = Depends(get_db),
):
    """
    Create or update a feature flag for the current tenant.

    Use this for blue/green deployments, algorithm rollouts (e.g. enable Bayesian
    scoring for 10% of workers first), and A/B testing.

    rollout_pct: percentage of workers (0–100) who see this flag as enabled.
    """
    from sqlalchemy import text
    await db.execute(
        text("""
            INSERT INTO feature_flags (tenant_id, flag_name, enabled, rollout_pct, metadata)
            VALUES (:tenant_id, :flag_name, :enabled, :rollout_pct, :metadata::jsonb)
            ON CONFLICT (tenant_id, flag_name)
            DO UPDATE SET
                enabled     = EXCLUDED.enabled,
                rollout_pct = EXCLUDED.rollout_pct,
                metadata_    = EXCLUDED.metadata_,
                updated_at  = now()
        """),
        {
            "tenant_id":   auth.tenant_id,
            "flag_name":   flag.flag_name,
            "enabled":     flag.enabled,
            "rollout_pct": flag.rollout_pct,
            "metadata":    __import__("json").dumps(flag.metadata),
        },
    )
    await db.commit()
    return flag

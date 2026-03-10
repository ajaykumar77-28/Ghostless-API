"""
Ghostless API — Unified Decision Pipeline (v6)

Merges rule_engine + scoring into a single, auditable, pure-function pipeline.

Previous design had two separate modules (rule_engine.py, scoring.py) that were
called sequentially with no shared context. This caused:
  - Duplicate z-score and velocity lookups
  - No single "source of truth" for the decision
  - Impossible to unit-test the end-to-end outcome

This pipeline:
  1. Validates the payload (rule checks)
  2. Runs fraud signal detectors
  3. Calls the Bayesian scoring engine
  4. Produces a single PipelineDecision with full explainability

It is intentionally a pure function: all I/O (DB reads, Redis reads) must
happen in the caller (router or task). The pipeline receives pre-loaded
context via PipelineContext and returns PipelineDecision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.engine.bayesian import (
    BayesianScoreInput,
    BayesianScoreResult,
    compute_bayesian_score,
    PRIOR_ALPHA,
    PRIOR_BETA,
)
from app.services.fraud import (
    FraudSignal,
    check_velocity,
    check_payload_entropy,
    check_answer_similarity,
    check_ip_sharing,
    check_speed_anomaly,
)
from app.services.rule_engine import RuleEngine, RuleResult

# ─── Shared RuleEngine instance ───────────────────────────────────────────────
_rule_engine = RuleEngine()


# ─── Context (caller populates this from DB/Redis before calling) ─────────────

@dataclass
class WorkerContext:
    """Everything the pipeline needs to know about a worker right now."""
    external_id:            str
    trust_score:            float = 50.0
    posterior_alpha:        float = PRIOR_ALPHA
    posterior_beta:         float = PRIOR_BETA
    total_tasks:            int   = 0
    streak_days:            int   = 0
    days_since_joined:      float = 0.0
    max_trust:              float = 100.0
    shadow_banned:          bool  = False
    lifecycle_stage:        str   = "new"
    volatility:             float = 0.0
    fraud_multiplier:       float = 1.0     # pre-computed from FraudEvent rows

    # Anti-farming context: most recent task_types + accepted flags
    recent_task_types:      List[str]  = field(default_factory=list)
    recent_accepted:        List[bool] = field(default_factory=list)

    # Velocity
    tasks_last_hour:        int   = 0
    tasks_last_day:         int   = 0
    max_tasks_per_hour:     int   = 60
    max_tasks_per_day:      int   = 300

    # Peer baseline
    peer_zscore:            Optional[float] = None
    peer_n:                 int   = 0

    # Recent payload hashes (for answer-similarity)
    recent_payload_hashes:  List[str] = field(default_factory=list)

    # IP sharing
    ip_address:             str        = ""
    other_workers_on_ip:    List[str]  = field(default_factory=list)

    # Speed baseline (from peer Welford)
    time_baseline_mean:     float = 0.0
    time_baseline_std:      float = 0.0

    # Average completion time (for speed ratio check)
    avg_completion_time:    float = 120.0


@dataclass
class TaskContext:
    """Details of the task submission being evaluated."""
    task_type:          str
    payload:            Dict[str, Any]
    completion_time:    float
    difficulty_weight:  float = 1.0
    was_accepted:       Optional[bool] = None    # None until graded


# ─── Output ───────────────────────────────────────────────────────────────────

@dataclass
class PipelineDecision:
    """Full output of one pipeline run — single source of truth."""

    # Final scores
    quality_score:          float         # 0–1 from rule engine
    trust_score:            float         # 0–100 Bayesian trust
    trust_delta:            float

    # Bayesian state to persist
    bayesian_result:        BayesianScoreResult

    # Rule engine output
    rule_result:            RuleResult

    # Fraud signals
    fraud_signals:          List[FraudSignal]
    fraud_severity:         str           # "clean" | "warning" | "critical"
    auto_action:            Optional[str] # None | "flag" | "suspend" | "ban"

    # Submission decision
    allow_submit:           bool
    shadow_banned:          bool

    # Anomaly
    anomaly_score:          float

    # Velocity
    velocity_warning:       bool
    velocity_ratio:         float

    # Explainability (human-readable breakdown)
    explanation:            Dict[str, Any] = field(default_factory=dict)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_pipeline(
    worker: WorkerContext,
    task:   TaskContext,
    min_quality_to_submit: float = 0.30,
    speed_flag_pct:        float = 0.15,
    speed_warn_pct:        float = 0.35,
) -> PipelineDecision:
    """
    Run the full validation → fraud → scoring pipeline. Pure function.

    Args:
        worker: Pre-loaded worker context (from DB/Redis).
        task:   The task submission being evaluated.
        min_quality_to_submit: Reject below this quality score.
        speed_flag_pct: Fraction of avg_completion_time that triggers an error flag.
        speed_warn_pct: Fraction of avg_completion_time that triggers a warning.

    Returns:
        PipelineDecision with full explainability and persist-ready Bayesian state.
    """

    # ── 1. Rule engine ─────────────────────────────────────────────────────
    # Inject speed thresholds via a simple settings-like dict override
    worker_history = {
        "trust_score":         worker.trust_score,
        "avg_completion_time": worker.avg_completion_time,
        "total_tasks":         worker.total_tasks,
        "accuracy_30d":        None,
        "tier":                "bronze",
    }
    rule_result = _rule_engine.run(
        task_type=task.task_type,
        payload=task.payload,
        completion_time=task.completion_time,
        worker_history=worker_history,
    )
    quality_score = rule_result.quality_score

    # ── 2. Fraud signal collection ─────────────────────────────────────────
    fraud_signals: List[FraudSignal] = []

    # Velocity
    fraud_signals += check_velocity(
        worker.tasks_last_hour,
        worker.tasks_last_day,
        worker.max_tasks_per_hour,
        worker.max_tasks_per_day,
    )

    # Entropy
    fraud_signals += check_payload_entropy(task.payload)

    # Answer similarity
    fraud_signals += check_answer_similarity(task.payload, worker.recent_payload_hashes)

    # IP sharing
    if worker.ip_address:
        fraud_signals += check_ip_sharing(
            worker.ip_address,
            worker.other_workers_on_ip,
            worker.external_id,
        )

    # Speed anomaly (vs peer baseline)
    if worker.time_baseline_mean > 0:
        fraud_signals += check_speed_anomaly(
            task.completion_time,
            worker.time_baseline_mean,
            worker.time_baseline_std,
        )

    # Sybil resistance: flag if many workers on same IP + low peer_n
    if worker.ip_address and len(worker.other_workers_on_ip) > 2 and worker.peer_n < 10:
        fraud_signals.append(FraudSignal(
            event_type  = "sybil_suspect",
            severity    = "warning",
            details     = {
                "ip_workers": len(worker.other_workers_on_ip),
                "peer_n":     worker.peer_n,
            },
        ))

    # Circular trust: penalize if worker is both worker and assessor in same task_type
    # (Detected upstream and signalled via worker_context.fraud_multiplier being < 1.0)

    # ── 3. Fraud severity summary ──────────────────────────────────────────
    has_critical = any(s.severity == "critical" for s in fraud_signals)
    has_warning  = any(s.severity == "warning"  for s in fraud_signals)
    fraud_severity = "critical" if has_critical else ("warning" if has_warning else "clean")

    auto_actions = [s.auto_action for s in fraud_signals if s.auto_action]
    auto_action  = "suspend" if "suspend" in auto_actions else (
                   "flag"    if "flag"    in auto_actions else None
                   )

    # ── 4. Velocity ratio ─────────────────────────────────────────────────
    velocity_ratio = worker.tasks_last_hour / max(worker.max_tasks_per_hour, 1)

    # ── 5. Anomaly score (composite) ──────────────────────────────────────
    import json
    import math
    raw_text = json.dumps(task.payload, sort_keys=True)

    def _shannon(s: str) -> float:
        if not s:
            return 0.0
        from collections import Counter
        counts = Counter(s)
        n = len(s)
        return -sum((c / n) * math.log2(c / n) for c in counts.values())

    entropy = _shannon(raw_text) if len(raw_text) >= 20 else 4.0
    entropy_norm = min(1.0, entropy / 4.0)

    z_component = 0.0
    if worker.peer_zscore is not None:
        z_component = min(1.0, max(0.0, (abs(worker.peer_zscore) - 1.0) / 3.0))
    v_component = min(1.0, velocity_ratio)
    e_component = max(0.0, 1.0 - entropy_norm)
    anomaly_score = round(
        z_component * 0.50 + v_component * 0.25 + e_component * 0.25, 4
    )

    # ── 6. Velocity penalty in trust points ────────────────────────────────
    vel_ratio = velocity_ratio
    if vel_ratio < 0.5:
        vel_penalty_pts = 0.0
    else:
        excess = (vel_ratio - 0.5) / 0.5
        vel_penalty_pts = round(min(15.0, 15.0 * excess), 2)

    # ── 7. Streak and tenure bonuses ──────────────────────────────────────
    streak_bonus = round(min(worker.streak_days, 30) / 30.0 * 10.0, 2)
    tenure_bonus = round(min(worker.days_since_joined, 365) / 365.0 * 5.0, 2)

    # ── 8. Bayesian trust update ───────────────────────────────────────────
    bayesian_input = BayesianScoreInput(
        posterior_alpha       = worker.posterior_alpha,
        posterior_beta        = worker.posterior_beta,
        was_accepted          = task.was_accepted,
        task_type             = task.task_type,
        difficulty_weight     = task.difficulty_weight,
        zscore_vs_cohort      = worker.peer_zscore,
        peer_n                = worker.peer_n,
        recent_task_types     = worker.recent_task_types,
        recent_accepted       = worker.recent_accepted,
        fraud_multiplier      = worker.fraud_multiplier,
        velocity_penalty_pts  = vel_penalty_pts,
        streak_bonus_pts      = streak_bonus,
        tenure_bonus_pts      = tenure_bonus,
        total_observations    = worker.total_tasks,
        current_trust         = worker.trust_score,
        current_volatility    = worker.volatility,
        max_trust             = worker.max_trust,
    )
    bayesian_result = compute_bayesian_score(bayesian_input)

    # ── 9. Submit decision ─────────────────────────────────────────────────
    has_rule_errors = any(w.severity == "error" for w in rule_result.warnings)
    allow_submit    = (
        not has_rule_errors
        and quality_score >= min_quality_to_submit
        and not has_critical     # critical fraud signal blocks submission
        and auto_action != "suspend"
    )

    # ── 10. Explainability ─────────────────────────────────────────────────
    explanation = {
        "algorithm_version":        bayesian_result.algorithm_version,
        "posterior_mean":           bayesian_result.posterior_mean,
        "ci_90pct":                 [bayesian_result.ci_lower, bayesian_result.ci_upper],
        "ci_width":                 bayesian_result.ci_width,
        "base_trust_from_posterior": bayesian_result.base_trust_from_posterior,
        "cold_start_factor":        bayesian_result.cold_start_factor,
        "farming_weight":           bayesian_result.farming_weight,
        "peer_adjustment_pts":      bayesian_result.peer_adjustment_pts,
        "streak_bonus_pts":         bayesian_result.streak_component,
        "tenure_bonus_pts":         bayesian_result.tenure_component,
        "velocity_penalty_pts":     bayesian_result.velocity_penalty,
        "fraud_multiplier":         bayesian_result.fraud_multiplier,
        "trust_delta":              bayesian_result.trust_delta,
        "volatility":               bayesian_result.volatility,
        "rule_flags":               rule_result.flags,
        "fraud_signals":            [
            {"type": s.event_type, "severity": s.severity}
            for s in fraud_signals
        ],
        "anomaly_score":            anomaly_score,
        "quality_score":            quality_score,
    }

    return PipelineDecision(
        quality_score  = round(quality_score, 4),
        trust_score    = bayesian_result.trust_score,
        trust_delta    = bayesian_result.trust_delta,
        bayesian_result = bayesian_result,
        rule_result    = rule_result,
        fraud_signals  = fraud_signals,
        fraud_severity = fraud_severity,
        auto_action    = auto_action,
        allow_submit   = allow_submit,
        shadow_banned  = worker.shadow_banned,
        anomaly_score  = anomaly_score,
        velocity_warning = velocity_ratio > 0.8,
        velocity_ratio = round(velocity_ratio, 3),
        explanation    = explanation,
    )

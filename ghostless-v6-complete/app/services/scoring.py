"""
Ghostless API — Real Scoring Engine (v5)

New in v5:
  - Weighted baseline updates (soft contribution weighting, not hard 30% rejection)
  - Baseline drift detection (compare current mean to rolling snapshot)
  - Minimum peer count before confidence can exceed thresholds
  - Per-task EWMA influence cap (prevents trivial-task streak amplification)
  - Accuracy history decay (older accepted tasks count less, half-life configurable)
  - Task difficulty weighting (high-difficulty tasks move trust more)
  - Worker velocity penalty directly in trust score (not only suspension)
  - Long-horizon coordinated fraud signals feed into fraud multiplier
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ─── Constants (defaults; per-tenant overrides loaded at call site) ────────────

EWMA_ALPHA_NEW         = 0.30
EWMA_ALPHA_MATURE      = 0.10
EWMA_ALPHA_DECAY_N     = 20

ZSCORE_ANOMALY_THRESHOLD = 2.5
ZSCORE_FRAUD_THRESHOLD   = 3.5
ZSCORE_MIN_PEER_SAMPLE   = 30

CONFIDENCE_FLOOR     = 0.40
CONFIDENCE_CEIL      = 1.00
CONFIDENCE_FULL_AT_N = 50
# Minimum peer counts for confidence gates
MIN_PEERS_FOR_CONF_50 = 10    # peer baseline n < 10 → conf capped at CONFIDENCE_FLOOR
MIN_PEERS_FOR_CONF_80 = 30    # peer baseline n < 30 → conf capped at 0.80

FRAUD_MULTIPLIER_PER_EVENT = 0.5
FRAUD_MAX_EVENTS_COUNTED   = 3
FRAUD_HALFLIFE_DAYS        = 90.0

MAX_WORKER_BASELINE_WEIGHT = 0.30   # soft max weight per worker in weighted baseline
POST_FRAUD_MAX_TRUST       = 70.0

# V5: velocity trust penalty (points subtracted before multipliers)
VELOCITY_PENALTY_MAX = 15.0

# V5: max EWMA delta per single task (clamps extreme quality swings)
MAX_EWMA_DELTA_PER_TASK = 0.05

# V5: accuracy history decay half-life in days
ACCURACY_HALFLIFE_DAYS = 60.0

# V5: baseline drift — alert if current mean deviates this many std from snapshot
BASELINE_DRIFT_THRESHOLD = 2.0

# V5: anomaly composite score weight factors
ANOMALY_ZSCORE_WEIGHT    = 0.50
ANOMALY_VELOCITY_WEIGHT  = 0.25
ANOMALY_ENTROPY_WEIGHT   = 0.25


# ─── Weighted Welford baseline (v5) ───────────────────────────────────────────

def welford_update_weighted(
    existing: dict,
    new_value: float,
    worker_contribution: int = 0,
    baseline_total: int = 0,
    worker_weight_cap: float = MAX_WORKER_BASELINE_WEIGHT,
) -> dict:
    """
    Weighted Welford update. Instead of hard-rejecting high-contribution workers,
    this applies a soft weight that diminishes as the worker contributes more.

    weight = max(0.1, 1.0 - (worker_contribution / baseline_total) / worker_weight_cap)

    This means a worker who already owns 30% of the baseline still contributes,
    but each new observation counts for less rather than being silently dropped.
    """
    n = existing.get("n", 0)
    if n == 0 or baseline_total == 0:
        # First observation — full weight
        weight = 1.0
    else:
        fraction = (worker_contribution) / max(baseline_total, 1)
        weight = max(0.1, 1.0 - fraction / worker_weight_cap)

    # Weighted Welford update
    mean  = existing.get("mean", 0.0)
    m2    = existing.get("m2",   0.0)
    total = existing.get("n",    0)

    total  += weight
    delta   = new_value - mean
    mean   += (delta * weight) / total
    delta2  = new_value - mean
    m2     += weight * delta * delta2

    return {"mean": mean, "m2": m2, "n": total}


def welford_std(baseline: dict) -> float:
    n  = baseline.get("n", 0)
    m2 = baseline.get("m2", 0.0)
    if n < 2:
        return 0.0
    return math.sqrt(m2 / max(n, 1))


# ─── Baseline drift detection ─────────────────────────────────────────────────

def detect_baseline_drift(
    current_baseline: dict,
    snapshot_baseline: dict,
    threshold: float = BASELINE_DRIFT_THRESHOLD,
) -> dict:
    """
    Compare current baseline mean to a saved snapshot.
    Returns {"drift_detected": bool, "delta_sigma": float, "direction": str}.

    A large mean shift could indicate poisoning or genuine distribution change.
    """
    if not snapshot_baseline or snapshot_baseline.get("n", 0) < ZSCORE_MIN_PEER_SAMPLE:
        return {"drift_detected": False, "delta_sigma": 0.0, "direction": "none"}

    snap_mean = snapshot_baseline.get("mean", 0.0)
    snap_std  = welford_std(snapshot_baseline)
    if snap_std < 1e-6:
        return {"drift_detected": False, "delta_sigma": 0.0, "direction": "none"}

    curr_mean = current_baseline.get("mean", snap_mean)
    delta     = curr_mean - snap_mean
    delta_sigma = abs(delta) / snap_std

    drift_detected = delta_sigma > threshold
    return {
        "drift_detected": drift_detected,
        "delta_sigma":    round(delta_sigma, 3),
        "direction":      "up" if delta > 0 else "down",
        "snap_mean":      round(snap_mean, 4),
        "curr_mean":      round(curr_mean, 4),
    }


# ─── EWMA helpers ─────────────────────────────────────────────────────────────

def adaptive_alpha(total_tasks: int, alpha_new: float = EWMA_ALPHA_NEW,
                   alpha_mature: float = EWMA_ALPHA_MATURE,
                   decay_n: int = EWMA_ALPHA_DECAY_N) -> float:
    if total_tasks >= decay_n:
        return alpha_mature
    t = total_tasks / decay_n
    return alpha_new + t * (alpha_mature - alpha_new)


def update_ewma_capped(
    current_ewma: float,
    new_value: float,
    alpha: float,
    difficulty: float = 1.0,
    max_delta: float = MAX_EWMA_DELTA_PER_TASK,
) -> float:
    """
    Standard EWMA with per-task delta cap.

    V5: Cap on how much a single task can move EWMA prevents trivial-task spam
    from amplifying trust. High-difficulty tasks get larger caps.
    """
    raw_new    = alpha * new_value + (1 - alpha) * current_ewma
    raw_delta  = raw_new - current_ewma
    # Scale cap by difficulty: hard tasks move score more
    eff_cap    = max_delta * max(0.5, min(difficulty, 3.0))
    clamped    = current_ewma + max(-eff_cap, min(eff_cap, raw_delta))
    return clamped


# ─── Confidence weighting (v5) ────────────────────────────────────────────────

def confidence_weight(
    accepted_tasks: int,
    peer_baseline_n: int = 0,
    min_peers_50: int = MIN_PEERS_FOR_CONF_50,
    min_peers_80: int = MIN_PEERS_FOR_CONF_80,
) -> float:
    """
    V5: Added peer-count gate. Confidence cannot exceed:
      - CONFIDENCE_FLOOR unless peer baseline has >= min_peers_50 observations
      - 0.80 unless peer baseline has >= min_peers_80 observations

    This prevents early consensus inflation when a task type is new.
    """
    if accepted_tasks >= CONFIDENCE_FULL_AT_N:
        raw_conf = CONFIDENCE_CEIL
    else:
        t = accepted_tasks / CONFIDENCE_FULL_AT_N
        raw_conf = CONFIDENCE_FLOOR + t * (CONFIDENCE_CEIL - CONFIDENCE_FLOOR)

    # Apply peer-count gates
    if peer_baseline_n < min_peers_50:
        return min(raw_conf, CONFIDENCE_FLOOR)
    if peer_baseline_n < min_peers_80:
        return min(raw_conf, 0.80)
    return raw_conf


# ─── Accuracy with time decay (v5) ────────────────────────────────────────────

def decayed_accuracy(
    accepted_weights: float,
    total_weights: float,
) -> Optional[float]:
    """
    V5: Accuracy computed from pre-decayed weights (caller computes via SQL).
    Older accepted/rejected tasks contribute less using exponential decay.
    Returns None if no weighted tasks available.
    """
    if total_weights <= 0:
        return None
    return round(accepted_weights / total_weights * 100.0, 2)


# ─── Z-score calculation ──────────────────────────────────────────────────────

def compute_zscore(score: float, baseline: dict) -> Optional[float]:
    n   = baseline.get("n", 0)
    std = welford_std(baseline)
    if n < ZSCORE_MIN_PEER_SAMPLE or std < 1e-6:
        return None
    return (score - baseline["mean"]) / std


# ─── Anomaly composite score (v5) ─────────────────────────────────────────────

def compute_anomaly_score(
    zscore: Optional[float],
    velocity_ratio: float = 0.0,   # tasks_last_hour / max_per_hour (0–1+)
    entropy_score: float = 1.0,    # 1.0 = normal, 0.0 = maximally low entropy
) -> float:
    """
    V5: Composite anomaly score (0–1). Persisted on each validation row.
    Combines z-score, velocity, and payload entropy signals.
    """
    z_component = 0.0
    if zscore is not None:
        # Normalize z-score: z=2.5 → 0.5, z=3.5 → 1.0
        z_component = min(1.0, max(0.0, (abs(zscore) - 1.0) / 3.0))

    v_component = min(1.0, velocity_ratio)
    e_component = max(0.0, 1.0 - entropy_score)   # low entropy = high anomaly

    return round(
        z_component  * ANOMALY_ZSCORE_WEIGHT +
        v_component  * ANOMALY_VELOCITY_WEIGHT +
        e_component  * ANOMALY_ENTROPY_WEIGHT,
        4,
    )


# ─── Fraud multipliers (v5) ───────────────────────────────────────────────────

def effective_fraud_multiplier(fraud_events_aged: list) -> float:
    """
    Multiplicative fraud decay. Each event halves trust with time decay.
    fraud_events_aged: [{"age_days": float}, ...]
    """
    if not fraud_events_aged:
        return 1.0
    total_weight = sum(
        2 ** (-e.get("age_days", 0) / FRAUD_HALFLIFE_DAYS)
        for e in fraud_events_aged
    )
    capped = min(total_weight, FRAUD_MAX_EVENTS_COUNTED)
    return FRAUD_MULTIPLIER_PER_EVENT ** capped


def simple_fraud_multiplier(count: int) -> float:
    if count <= 0:
        return 1.0
    return FRAUD_MULTIPLIER_PER_EVENT ** min(count, FRAUD_MAX_EVENTS_COUNTED)


# ─── Velocity penalty in trust (v5) ───────────────────────────────────────────

def velocity_trust_penalty(
    tasks_last_hour: int,
    max_per_hour: int,
    penalty_max: float = VELOCITY_PENALTY_MAX,
) -> float:
    """
    V5: Velocity pattern directly penalises trust score (before multipliers).
    A worker at 90% of hourly cap loses penalty_max * 0.9 points.
    At 100%+ cap, full penalty applied. Below 50%, no penalty.
    """
    ratio = tasks_last_hour / max(max_per_hour, 1)
    if ratio < 0.5:
        return 0.0
    excess = (ratio - 0.5) / 0.5   # 0 at 50% load, 1.0 at 100% load
    return round(min(penalty_max, penalty_max * excess), 2)


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ScoreUpdateInput:
    quality_score:        float
    task_type:            str
    was_accepted:         Optional[bool] = None
    _fraud_event_count:   int   = 0        # sourced from DB only (FIX #13)
    streak_days:          int   = 0
    days_since_joined:    float = 0.0
    current_ewma:         float = 0.5
    current_total_tasks:  int   = 0
    current_accepted:     int   = 0
    # SQL-computed window values (FIX #1)
    current_acc_7d_n:     int   = 0
    current_acc_7d_sum:   int   = 0
    current_acc_30d_n:    int   = 0
    current_acc_30d_sum:  int   = 0
    current_acc_all_n:    int   = 0
    current_acc_all_sum:  int   = 0
    # V5: decayed accuracy weights (from SQL EXP decay)
    acc_30d_decayed_accepted: float = 0.0
    acc_30d_decayed_total:    float = 0.0
    task_baselines:       dict  = field(default_factory=dict)
    baseline_snapshots:   dict  = field(default_factory=dict)   # {task_type: baseline_dict}
    current_zscore_flagged: int = 0
    worker_baseline_contribution: int = 0
    max_trust:            float = 100.0
    fraud_events_aged:    list  = field(default_factory=list)
    # V5: new inputs
    difficulty_weight:    float = 1.0     # task difficulty multiplier
    tasks_last_hour:      int   = 0       # for velocity penalty
    max_tasks_per_hour:   int   = 60
    peer_baseline_n:      int   = 0       # total peer observations for this task type
    velocity_ratio:       float = 0.0     # tasks_last_hour / max_per_hour
    entropy_score:        float = 1.0     # payload entropy (1.0 = normal)


@dataclass
class ScoreUpdateResult:
    ewma_quality:         float
    ewma_alpha:           float
    trust_score:          float
    accuracy_7d:          Optional[float]
    accuracy_30d:         Optional[float]
    accuracy_all:         Optional[float]
    zscore_latest:        Optional[float]
    zscore_flagged_count: int
    confidence_weight_val: float
    fraud_multiplier:     float
    velocity_penalty:     float
    anomaly_score:        float
    task_baselines:       dict
    is_anomaly:           bool = False
    is_fraud_suspect:     bool = False
    # V5: explainability
    ewma_component:       float = 0.0
    accuracy_component:   float = 0.0
    streak_component:     float = 0.0
    tenure_component:     float = 0.0
    drift_result:         dict  = field(default_factory=dict)


# ─── Main scoring function ────────────────────────────────────────────────────

def compute_score_update(inp: ScoreUpdateInput) -> ScoreUpdateResult:
    """
    Pure-function trust score computation. No I/O. Fully testable.
    Returns full explainability breakdown in result.
    """
    # 1. Adaptive alpha
    alpha = adaptive_alpha(inp.current_total_tasks)

    # 2. FIX #2: z-score vs OLD baseline BEFORE updating it
    baselines    = dict(inp.task_baselines)
    old_baseline = baselines.get(inp.task_type, {"mean": 0.0, "m2": 0.0, "n": 0})

    zscore           = compute_zscore(inp.quality_score * 100, old_baseline)
    is_anomaly       = zscore is not None and abs(zscore) > ZSCORE_ANOMALY_THRESHOLD
    is_fraud_suspect = zscore is not None and abs(zscore) > ZSCORE_FRAUD_THRESHOLD
    new_zscore_flagged = inp.current_zscore_flagged + (1 if is_anomaly else 0)

    # 3. V5: Baseline drift detection
    snap = inp.baseline_snapshots.get(inp.task_type, {})
    drift_result = detect_baseline_drift(old_baseline, snap)

    # 4. V5: Weighted Welford update (soft contribution weighting)
    updated_baseline = welford_update_weighted(
        old_baseline,
        inp.quality_score * 100,
        worker_contribution=inp.worker_baseline_contribution,
        baseline_total=old_baseline.get("n", 0),
    )
    baselines[inp.task_type] = updated_baseline

    # 5. V5: EWMA with per-task delta cap (difficulty-scaled)
    new_ewma = update_ewma_capped(
        inp.current_ewma,
        inp.quality_score,
        alpha,
        difficulty=inp.difficulty_weight,
        max_delta=MAX_EWMA_DELTA_PER_TASK,
    )

    # 6. Accuracy windows (FIX #1: SQL-computed; V5: use decayed weights if available)
    def safe_rate(s: int, n: int) -> Optional[float]:
        return round(s / n * 100, 2) if n > 0 else None

    acc_7d_sum  = inp.current_acc_7d_sum  + (1 if inp.was_accepted is True else 0)
    acc_7d_n    = inp.current_acc_7d_n    + (1 if inp.was_accepted is not None else 0)
    acc_all_sum = inp.current_acc_all_sum + (1 if inp.was_accepted is True else 0)
    acc_all_n   = inp.current_acc_all_n   + (1 if inp.was_accepted is not None else 0)

    acc_7d  = safe_rate(acc_7d_sum, acc_7d_n)
    acc_all = safe_rate(acc_all_sum, acc_all_n)

    # V5: use decayed accuracy for 30d if available, else fall back to raw counter
    if inp.acc_30d_decayed_total > 0:
        acc_30d = decayed_accuracy(
            inp.acc_30d_decayed_accepted + (1.0 if inp.was_accepted is True else 0.0),
            inp.acc_30d_decayed_total    + (1.0 if inp.was_accepted is not None else 0.0),
        )
    else:
        acc_30d_sum = inp.current_acc_30d_sum + (1 if inp.was_accepted is True else 0)
        acc_30d_n   = inp.current_acc_30d_n   + (1 if inp.was_accepted is not None else 0)
        acc_30d = safe_rate(acc_30d_sum, acc_30d_n)

    # 7. FIX #5: Confidence — now also gated by peer count
    cw = confidence_weight(
        acc_all_n,
        peer_baseline_n=inp.peer_baseline_n,
    )

    # 8. V5: Velocity penalty directly in trust
    vel_penalty = velocity_trust_penalty(inp.tasks_last_hour, inp.max_tasks_per_hour)

    # 9. Streak and tenure bonuses
    streak_bonus = min(inp.streak_days, 30) / 30.0 * 10.0
    tenure_bonus = min(inp.days_since_joined, 365) / 365.0 * 5.0

    # 10. FIX #3 + #12: Multiplicative fraud penalty with time decay
    fraud_mult = (
        effective_fraud_multiplier(inp.fraud_events_aged)
        if inp.fraud_events_aged else
        simple_fraud_multiplier(inp._fraud_event_count)
    )

    # 11. V5: Task difficulty scales EWMA contribution
    ewma_component     = new_ewma * 100 * 0.60 * inp.difficulty_weight
    accuracy_component = (acc_30d or 50.0) * 0.25
    streak_component   = streak_bonus
    tenure_component   = tenure_bonus

    trust_raw = ewma_component + accuracy_component + streak_component + tenure_component
    # Subtract velocity penalty before multipliers
    trust_raw = trust_raw - vel_penalty
    trust_clamped = max(0.0, min(100.0, trust_raw))

    # FIX #5: confidence and fraud are both multiplicative on full score
    trust_score = round(trust_clamped * cw * fraud_mult, 2)
    # FIX #7: max_trust ceiling
    trust_score = round(min(trust_score, inp.max_trust), 2)

    # 12. V5: Composite anomaly score
    anomaly_score = compute_anomaly_score(
        zscore         = zscore,
        velocity_ratio = inp.velocity_ratio,
        entropy_score  = inp.entropy_score,
    )

    return ScoreUpdateResult(
        ewma_quality          = round(new_ewma, 6),
        ewma_alpha            = round(alpha, 4),
        trust_score           = trust_score,
        accuracy_7d           = acc_7d,
        accuracy_30d          = acc_30d,
        accuracy_all          = acc_all,
        zscore_latest         = round(zscore, 4) if zscore is not None else None,
        zscore_flagged_count  = new_zscore_flagged,
        confidence_weight_val = round(cw, 4),
        fraud_multiplier      = round(fraud_mult, 4),
        velocity_penalty      = round(vel_penalty, 2),
        anomaly_score         = anomaly_score,
        task_baselines        = baselines,
        is_anomaly            = is_anomaly,
        is_fraud_suspect      = is_fraud_suspect,
        # Explainability
        ewma_component        = round(ewma_component, 4),
        accuracy_component    = round(accuracy_component, 4),
        streak_component      = round(streak_component, 4),
        tenure_component      = round(tenure_component, 4),
        drift_result          = drift_result,
    )

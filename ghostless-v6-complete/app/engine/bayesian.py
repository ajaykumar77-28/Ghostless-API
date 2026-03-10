"""
Ghostless API — Bayesian Scoring Engine (v6)

Replaces the v5 weighted-average approach with proper Bayesian inference.

Key improvements over v5:
  - Beta distribution belief over task acceptance probability (conjugate prior)
  - Closed-form confidence intervals (Beta credible intervals)
  - Momentum limiting: score can't jump more than MAX_DELTA_PER_CYCLE
  - Cold-start uncertainty penalty (replaces confidence_weight hack)
  - Anti-farming: diminishing returns on repeated identical-action streaks
  - Monotonic constraint: negative signals can never be fully offset by positives
  - Score volatility (rolling std of trust deltas) tracked and exposed
  - Peer-relative z-score baked directly into the trust update (not just flagging)

Algorithm version is baked into every ScoreUpdateResult so historical
scores can be reproduced even after the algorithm changes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ── Algorithm identifier ─────────────────────────────────────────────────────
# Increment this whenever the scoring formula changes materially.
ALGORITHM_VERSION = "v6.0.0"

# ── Beta prior hyper-parameters ──────────────────────────────────────────────
# New workers start as Beta(2, 2) — weakly centred at 0.5, moderate uncertainty.
# These are tuned so that 5 accepted tasks are enough to start seeing trust grow.
PRIOR_ALPHA = 2.0   # pseudo-successes
PRIOR_BETA  = 2.0   # pseudo-failures

# ── Confidence interval ───────────────────────────────────────────────────────
CREDIBLE_INTERVAL_WIDTH = 0.90   # 90 % credible interval on Beta posterior

# ── Momentum / delta cap ─────────────────────────────────────────────────────
# Trust cannot change by more than this many points in a single scoring cycle
# regardless of how many tasks were processed.
MAX_TRUST_DELTA_PER_CYCLE = 8.0

# ── Cold-start penalty ────────────────────────────────────────────────────────
# Workers with < MIN_OBS_FOR_FULL_TRUST observations have their trust multiplied
# by uncertainty_factor (< 1.0), growing linearly to 1.0 at MIN_OBS_FOR_FULL_TRUST.
MIN_OBS_FOR_FULL_TRUST = 30

# ── Anti-farming ──────────────────────────────────────────────────────────────
# Each additional consecutive identical action (same task_type, same accepted flag)
# contributes at most FARMING_DECAY_BASE^n weight (n = run length).
FARMING_DECAY_BASE = 0.80
FARMING_MAX_LOOKBACK = 20   # only look back this many tasks for streak detection

# ── Monotonic constraint ──────────────────────────────────────────────────────
# When was_accepted is False, the resulting trust score cannot be higher than
# MONOTONIC_PENALTY_FLOOR × (score before the update).
MONOTONIC_REJECTION_FLOOR = 0.98   # a single rejection can never do less than -2%

# ── Peer-relative scaling ──────────────────────────────────────────────────────
# If worker's z-score vs cohort > PEER_BONUS_Z, add small trust bonus.
# If < -PEER_BONUS_Z, add trust penalty.
PEER_BONUS_Z       = 1.0
PEER_BONUS_POINTS  = 2.0    # max bonus/penalty in trust points

# ── Volatility tracking ───────────────────────────────────────────────────────
VOLATILITY_HALFLIFE_CYCLES = 10   # EWMA half-life (in scoring cycles) for volatility


# ─── Bayesian Beta helpers ────────────────────────────────────────────────────

def beta_mean(alpha: float, beta: float) -> float:
    """E[X] for Beta(alpha, beta)."""
    return alpha / (alpha + beta)


def beta_variance(alpha: float, beta: float) -> float:
    """Var[X] for Beta(alpha, beta)."""
    total = alpha + beta
    return (alpha * beta) / (total ** 2 * (total + 1))


def _regularized_incomplete_beta(x: float, a: float, b: float, n_terms: int = 200) -> float:
    """
    Numerical approximation of I_x(a, b) via continued fraction (Lentz method).
    Accurate to < 1e-7 for most (a, b, x) combinations.
    Used for credible interval calculation without scipy dependency.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Use the symmetry relation when x > (a+1)/(a+b+2) for better convergence
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _regularized_incomplete_beta(1.0 - x, b, a, n_terms)

    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta) / a

    # Lentz continued fraction
    cf = 1.0
    c  = 1.0
    d  = 1.0 - (a + b) * x / (a + 1.0)
    d  = 1.0 / d if abs(d) < 1e-30 else 1.0 / d
    cf = d

    for m in range(1, n_terms + 1):
        # Even step
        numerator = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = 1.0 + numerator * d
        d = 1e30 if abs(d) < 1e-30 else d
        c = 1.0 + numerator / c
        c = 1e30 if abs(c) < 1e-30 else c
        d = 1.0 / d
        cf *= c * d

        # Odd step
        numerator = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + numerator * d
        d = 1e30 if abs(d) < 1e-30 else d
        c = 1.0 + numerator / c
        c = 1e30 if abs(c) < 1e-30 else c
        d = 1.0 / d
        delta = c * d
        cf *= delta

        if abs(delta - 1.0) < 1e-7:
            break

    return front * cf


def beta_credible_interval(
    alpha: float,
    beta: float,
    width: float = CREDIBLE_INTERVAL_WIDTH,
) -> tuple[float, float]:
    """
    Return (lower, upper) equal-tailed credible interval for Beta(alpha, beta).
    Uses numerical inversion of the regularized incomplete beta function.

    Example: beta_credible_interval(10, 5, 0.90) → ~(0.48, 0.88)
    """
    tail = (1.0 - width) / 2.0

    def _find_quantile(p: float) -> float:
        # Binary search on [0, 1] for quantile p
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if _regularized_incomplete_beta(mid, alpha, beta) < p:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lower = _find_quantile(tail)
    upper = _find_quantile(1.0 - tail)
    return round(lower, 4), round(upper, 4)


# ─── Farming detection ────────────────────────────────────────────────────────

def anti_farming_weight(
    recent_task_types: list[str],    # most recent first
    recent_accepted: list[bool],     # corresponding accepted flags
    current_task_type: str,
    current_accepted: bool,
) -> float:
    """
    Returns a weight in (0, 1] for the current observation.
    Repeated identical (task_type, accepted) pairs see diminishing returns,
    capped at FARMING_DECAY_BASE^run_length.

    A worker who does 20 identical accepted image_label tasks in a row gets
    weight ≈ 0.80^20 ≈ 0.012 for the last task — barely affecting the score.
    """
    run_length = 0
    for t, a in zip(recent_task_types[:FARMING_MAX_LOOKBACK],
                    recent_accepted[:FARMING_MAX_LOOKBACK]):
        if t == current_task_type and a == current_accepted:
            run_length += 1
        else:
            break
    return FARMING_DECAY_BASE ** run_length


# ─── Cold-start uncertainty ───────────────────────────────────────────────────

def cold_start_factor(n_observations: int) -> float:
    """
    Returns a scalar in [0, 1] representing how much we trust the score.
    Grows linearly from 0.4 at n=0 to 1.0 at n=MIN_OBS_FOR_FULL_TRUST.
    """
    t = min(n_observations, MIN_OBS_FOR_FULL_TRUST) / MIN_OBS_FOR_FULL_TRUST
    return 0.4 + 0.6 * t


# ─── Volatility (EWMA of |delta|) ────────────────────────────────────────────

def update_volatility(
    current_volatility: float,
    new_delta: float,
) -> float:
    """
    Update EWMA of absolute trust delta.
    Volatility = EWMA(|Δtrust|) across scoring cycles.
    """
    alpha = 1.0 - 0.5 ** (1.0 / VOLATILITY_HALFLIFE_CYCLES)
    return round(alpha * abs(new_delta) + (1.0 - alpha) * current_volatility, 4)


# ─── Peer-relative adjustment ─────────────────────────────────────────────────

def peer_relative_adjustment(zscore: Optional[float]) -> float:
    """
    Convert a peer z-score into a small trust point adjustment.
    z > +1.0 → +2 pts; z < -1.0 → -2 pts; linear interpolation between.
    """
    if zscore is None:
        return 0.0
    clamped = max(-3.0, min(3.0, zscore))
    return round(clamped / 3.0 * PEER_BONUS_POINTS, 3)


# ─── Input / Output dataclasses ───────────────────────────────────────────────

@dataclass
class BayesianScoreInput:
    # Posterior parameters from prior scoring cycle (start from prior if new)
    posterior_alpha:       float = PRIOR_ALPHA
    posterior_beta:        float = PRIOR_BETA

    # Current task event
    was_accepted:          Optional[bool] = None
    task_type:             str = "default"
    difficulty_weight:     float = 1.0      # hard tasks update posterior more

    # Peer context
    zscore_vs_cohort:      Optional[float] = None
    peer_n:                int = 0          # how many peers in cohort

    # Anti-farming context (most recent first, not including current)
    recent_task_types:     list = field(default_factory=list)
    recent_accepted:       list = field(default_factory=list)

    # Fraud
    fraud_multiplier:      float = 1.0      # from fraud service (0–1)
    velocity_penalty_pts:  float = 0.0      # direct trust penalty from velocity

    # Supplementary score components (streak, tenure) — still additive
    streak_bonus_pts:      float = 0.0
    tenure_bonus_pts:      float = 0.0

    # History
    total_observations:    int = 0           # total scored tasks (for cold-start)
    current_trust:         float = 50.0      # trust score before this cycle
    current_volatility:    float = 0.0       # EWMA of |Δtrust|

    # Ceiling
    max_trust:             float = 100.0


@dataclass
class BayesianScoreResult:
    # Posterior update
    posterior_alpha:        float
    posterior_beta:         float
    posterior_mean:         float   # E[p] of Beta posterior

    # Credible interval on posterior_mean
    ci_lower:               float
    ci_upper:               float
    ci_width:               float

    # Trust score
    trust_score:            float
    trust_delta:            float        # change from current_trust
    volatility:             float        # updated EWMA volatility

    # Anti-farming
    farming_weight:         float

    # Cold-start
    cold_start_factor:      float

    # Peer adjustment
    peer_adjustment_pts:    float

    # Explainability
    base_trust_from_posterior: float     # 100 × posterior_mean (before bonuses/penalties)
    streak_component:       float
    tenure_component:       float
    velocity_penalty:       float
    fraud_multiplier:       float

    # Algorithm version — baked into every result
    algorithm_version:      str = ALGORITHM_VERSION


# ─── Main compute function ────────────────────────────────────────────────────

def compute_bayesian_score(inp: BayesianScoreInput) -> BayesianScoreResult:
    """
    Pure-function Bayesian trust score update. No I/O.

    Flow:
      1. Compute anti-farming weight for this observation
      2. Update Beta posterior (if was_accepted is not None)
      3. Apply difficulty and farming weight as effective observation weight
      4. Compute point estimate (posterior_mean) → base trust
      5. Apply cold-start shrinkage toward 50 (uncertainty penalty)
      6. Add peer-relative adjustment
      7. Add streak and tenure bonuses
      8. Subtract velocity penalty
      9. Apply fraud multiplier
     10. Clamp by momentum cap (MAX_TRUST_DELTA_PER_CYCLE)
     11. Apply max_trust ceiling
     12. Update volatility
    """
    # 1. Anti-farming weight
    farming_wt = anti_farming_weight(
        inp.recent_task_types,
        inp.recent_accepted,
        inp.task_type,
        inp.was_accepted if inp.was_accepted is not None else False,
    )

    # 2 + 3. Bayesian posterior update with effective weight
    effective_weight = farming_wt * inp.difficulty_weight
    alpha = inp.posterior_alpha
    beta  = inp.posterior_beta

    if inp.was_accepted is True:
        alpha = alpha + effective_weight
    elif inp.was_accepted is False:
        beta  = beta  + effective_weight
    # If was_accepted is None (not graded yet), posterior unchanged

    # 4. Point estimate → base trust in [0, 100]
    post_mean = beta_mean(alpha, beta)
    base_trust = post_mean * 100.0

    # 5. Cold-start shrinkage toward 50
    cs_factor = cold_start_factor(inp.total_observations)
    shrunk_trust = 50.0 + (base_trust - 50.0) * cs_factor

    # 6. Peer-relative adjustment
    peer_adj = peer_relative_adjustment(inp.zscore_vs_cohort)

    # 7. Streak + tenure bonuses
    trust_with_bonuses = shrunk_trust + peer_adj + inp.streak_bonus_pts + inp.tenure_bonus_pts

    # 8. Velocity penalty (direct subtraction before multipliers)
    trust_before_mult = trust_with_bonuses - inp.velocity_penalty_pts

    # 9. Fraud multiplier
    trust_after_fraud = trust_before_mult * inp.fraud_multiplier

    # Clamp to valid range before momentum check
    trust_after_fraud = max(0.0, min(100.0, trust_after_fraud))

    # 10. Monotonic constraint: a rejection can never reduce trust by less than 2%
    if inp.was_accepted is False:
        floor = inp.current_trust * MONOTONIC_REJECTION_FLOOR
        trust_after_fraud = min(trust_after_fraud, floor)

    # 10b. Momentum cap: no more than MAX_TRUST_DELTA_PER_CYCLE per cycle
    raw_delta = trust_after_fraud - inp.current_trust
    clamped_delta = max(-MAX_TRUST_DELTA_PER_CYCLE, min(MAX_TRUST_DELTA_PER_CYCLE, raw_delta))
    trust_final = inp.current_trust + clamped_delta

    # 11. max_trust ceiling
    trust_final = round(min(trust_final, inp.max_trust), 2)
    trust_final = max(0.0, trust_final)

    # 12. Volatility
    actual_delta = trust_final - inp.current_trust
    new_volatility = update_volatility(inp.current_volatility, actual_delta)

    # 13. Credible interval
    ci_lower, ci_upper = beta_credible_interval(alpha, beta)

    return BayesianScoreResult(
        posterior_alpha            = round(alpha, 6),
        posterior_beta             = round(beta, 6),
        posterior_mean             = round(post_mean, 6),
        ci_lower                   = ci_lower,
        ci_upper                   = ci_upper,
        ci_width                   = round(ci_upper - ci_lower, 4),
        trust_score                = trust_final,
        trust_delta                = round(actual_delta, 4),
        volatility                 = new_volatility,
        farming_weight             = round(farming_wt, 4),
        cold_start_factor          = round(cs_factor, 4),
        peer_adjustment_pts        = round(peer_adj, 4),
        base_trust_from_posterior  = round(base_trust, 4),
        streak_component           = round(inp.streak_bonus_pts, 4),
        tenure_component           = round(inp.tenure_bonus_pts, 4),
        velocity_penalty           = round(inp.velocity_penalty_pts, 4),
        fraud_multiplier           = round(inp.fraud_multiplier, 4),
        algorithm_version          = ALGORITHM_VERSION,
    )

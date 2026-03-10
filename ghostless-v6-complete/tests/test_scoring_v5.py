"""
v5 Scoring Engine Tests

Covers:
  - Weighted baseline updates (soft contribution weighting)
  - Baseline drift detection
  - Minimum peer count gates on confidence
  - EWMA delta cap (trivial-task spam prevention)
  - Accuracy history decay via decayed weights
  - Task difficulty weighting in trust
  - Velocity penalty in trust score
  - Anomaly composite score
  - Full score explainability fields
  - Mixed honest/bot population simulation
  - Baseline poisoning adversarial test
"""
import math
import pytest

from app.services.scoring import (
    ScoreUpdateInput, ScoreUpdateResult, compute_score_update,
    welford_update_weighted, welford_std, detect_baseline_drift,
    update_ewma_capped, confidence_weight, velocity_trust_penalty,
    compute_anomaly_score, effective_fraud_multiplier,
    ZSCORE_MIN_PEER_SAMPLE,
)


# ─── Helper ───────────────────────────────────────────────────────────────────

def make_input(**kwargs) -> ScoreUpdateInput:
    defaults = dict(
        quality_score=0.8,
        task_type="image_label",
        was_accepted=True,
        streak_days=5,
        days_since_joined=30.0,
        current_ewma=0.5,
        current_total_tasks=10,
        current_accepted=8,
        current_acc_7d_n=8,  current_acc_7d_sum=7,
        current_acc_30d_n=8, current_acc_30d_sum=7,
        current_acc_all_n=8, current_acc_all_sum=7,
        task_baselines={},
        current_zscore_flagged=0,
        max_trust=100.0,
        fraud_events_aged=[],
        difficulty_weight=1.0,
        tasks_last_hour=10,
        max_tasks_per_hour=60,
        peer_baseline_n=50,
    )
    defaults.update(kwargs)
    return ScoreUpdateInput(**defaults)


# ─── Weighted Welford baseline ────────────────────────────────────────────────

class TestWeightedWelford:
    def test_first_observation_full_weight(self):
        s = welford_update_weighted({}, 80.0, worker_contribution=0, baseline_total=0)
        assert s["n"] == pytest.approx(1.0, abs=0.01)
        assert s["mean"] == pytest.approx(80.0)

    def test_high_contributor_soft_downweighted(self):
        # Worker has contributed 30% of 100 observations — weight should be reduced
        existing = {"mean": 80.0, "m2": 100.0, "n": 100.0}
        result = welford_update_weighted(existing, 50.0,   # outlier value
                                          worker_contribution=30, baseline_total=100)
        # Mean should move less than with full weight
        full_weight = welford_update_weighted(existing, 50.0, worker_contribution=0, baseline_total=0)
        # The high-contributor update should move mean less toward outlier
        assert abs(result["mean"] - 80.0) < abs(full_weight["mean"] - 80.0)

    def test_extreme_contributor_still_updates(self):
        # Even at 90% contribution, update is accepted (just downweighted, not rejected)
        existing = {"mean": 80.0, "m2": 200.0, "n": 100.0}
        result = welford_update_weighted(existing, 50.0, worker_contribution=90, baseline_total=100)
        # n increased (update was accepted)
        assert result["n"] > existing["n"]

    def test_normal_contributor_full_weight(self):
        # Worker at 5% contribution gets ~full weight
        existing = {"mean": 80.0, "m2": 200.0, "n": 100.0}
        r1 = welford_update_weighted(existing, 85.0, worker_contribution=5, baseline_total=100)
        r2 = welford_update_weighted(existing, 85.0, worker_contribution=0, baseline_total=0)
        assert abs(r1["mean"] - r2["mean"]) < 0.1


# ─── Baseline drift detection ─────────────────────────────────────────────────

class TestBaselineDrift:
    def _make_baseline(self, mean, std, n):
        m2 = std ** 2 * n
        return {"mean": mean, "m2": m2, "n": n}

    def test_no_drift_when_means_match(self):
        snap = self._make_baseline(80.0, 10.0, 100)
        curr = self._make_baseline(80.5, 10.0, 150)
        result = detect_baseline_drift(curr, snap, threshold=2.0)
        assert result["drift_detected"] == False

    def test_drift_detected_on_large_shift(self):
        snap = self._make_baseline(80.0, 5.0, 100)
        curr = self._make_baseline(94.0, 5.0, 150)   # 2.8 sigma shift
        result = detect_baseline_drift(curr, snap, threshold=2.0)
        assert result["drift_detected"] == True
        assert result["delta_sigma"] > 2.0

    def test_direction_up(self):
        snap = self._make_baseline(50.0, 10.0, 100)
        curr = self._make_baseline(80.0, 10.0, 150)
        result = detect_baseline_drift(curr, snap)
        assert result["direction"] == "up"

    def test_direction_down(self):
        snap = self._make_baseline(80.0, 10.0, 100)
        curr = self._make_baseline(50.0, 10.0, 150)
        result = detect_baseline_drift(curr, snap)
        assert result["direction"] == "down"

    def test_no_drift_when_snapshot_too_small(self):
        snap = {"mean": 80.0, "m2": 0.0, "n": 5}   # n < ZSCORE_MIN_PEER_SAMPLE
        curr = {"mean": 60.0, "m2": 100.0, "n": 100}
        result = detect_baseline_drift(curr, snap)
        assert result["drift_detected"] == False


# ─── Confidence peer-count gate ───────────────────────────────────────────────

class TestConfidencePeerGate:
    def test_new_task_type_caps_confidence(self):
        # Only 5 peer observations for this task type
        cw = confidence_weight(accepted_tasks=100, peer_baseline_n=5, min_peers_50=10)
        # Should be capped at CONFIDENCE_FLOOR = 0.40
        assert cw == pytest.approx(0.40)

    def test_enough_peers_for_50_threshold(self):
        cw = confidence_weight(accepted_tasks=100, peer_baseline_n=15, min_peers_50=10, min_peers_80=30)
        # 15 peers → can go up to 0.80 but not above
        assert cw <= 0.80
        assert cw > 0.40

    def test_full_peers_no_cap(self):
        cw = confidence_weight(accepted_tasks=100, peer_baseline_n=100, min_peers_50=10, min_peers_80=30)
        assert cw == pytest.approx(1.0)

    def test_mature_worker_new_task_still_capped(self):
        # Veteran worker (1000 tasks) but task type is brand new (5 peers)
        cw = confidence_weight(accepted_tasks=1000, peer_baseline_n=5, min_peers_50=10)
        assert cw == pytest.approx(0.40)


# ─── EWMA delta cap ───────────────────────────────────────────────────────────

class TestEWMADeltaCap:
    def test_small_change_uncapped(self):
        new_ewma = update_ewma_capped(0.7, 0.72, alpha=0.3, difficulty=1.0, max_delta=0.05)
        assert abs(new_ewma - 0.7) < 0.05

    def test_large_jump_capped(self):
        # Quality drops from 0.9 to 0.0 — should be capped
        capped = update_ewma_capped(0.9, 0.0, alpha=0.3, difficulty=1.0, max_delta=0.05)
        raw    = 0.3 * 0.0 + 0.7 * 0.9   # = 0.63
        # Raw delta would be 0.63 - 0.9 = -0.27, but cap is 0.05
        assert abs(capped - 0.9) <= 0.05 + 1e-9

    def test_hard_task_larger_cap(self):
        cap_normal = 0.9 - update_ewma_capped(0.9, 0.0, alpha=0.3, difficulty=1.0, max_delta=0.05)
        cap_hard   = 0.9 - update_ewma_capped(0.9, 0.0, alpha=0.3, difficulty=3.0, max_delta=0.05)
        # Hard task allows larger EWMA shift
        assert cap_hard >= cap_normal

    def test_easy_task_smaller_cap(self):
        cap_normal = abs(0.9 - update_ewma_capped(0.9, 0.0, alpha=0.3, difficulty=1.0, max_delta=0.05))
        cap_easy   = abs(0.9 - update_ewma_capped(0.9, 0.0, alpha=0.3, difficulty=0.5, max_delta=0.05))
        assert cap_easy <= cap_normal


# ─── Velocity penalty in trust ────────────────────────────────────────────────

class TestVelocityPenalty:
    def test_no_penalty_below_50pct(self):
        penalty = velocity_trust_penalty(tasks_last_hour=20, max_per_hour=60)
        assert penalty == pytest.approx(0.0)

    def test_penalty_at_full_capacity(self):
        penalty = velocity_trust_penalty(tasks_last_hour=60, max_per_hour=60)
        assert penalty == pytest.approx(15.0)

    def test_partial_penalty_at_80pct(self):
        penalty = velocity_trust_penalty(tasks_last_hour=48, max_per_hour=60)  # 80%
        assert 0 < penalty < 15.0

    def test_velocity_reduces_trust(self):
        low_vel  = compute_score_update(make_input(tasks_last_hour=5,  max_tasks_per_hour=60))
        high_vel = compute_score_update(make_input(tasks_last_hour=55, max_tasks_per_hour=60))
        assert low_vel.trust_score > high_vel.trust_score


# ─── Anomaly composite score ──────────────────────────────────────────────────

class TestAnomalyScore:
    def test_normal_submission_low_anomaly(self):
        score = compute_anomaly_score(zscore=0.5, velocity_ratio=0.1, entropy_score=0.9)
        assert score < 0.2

    def test_high_zscore_high_anomaly(self):
        score = compute_anomaly_score(zscore=4.0, velocity_ratio=0.0, entropy_score=1.0)
        assert score > 0.4

    def test_high_velocity_contributes(self):
        low_vel  = compute_anomaly_score(zscore=0.0, velocity_ratio=0.0, entropy_score=1.0)
        high_vel = compute_anomaly_score(zscore=0.0, velocity_ratio=1.0, entropy_score=1.0)
        assert high_vel > low_vel

    def test_low_entropy_contributes(self):
        normal = compute_anomaly_score(zscore=None, velocity_ratio=0.0, entropy_score=1.0)
        bot    = compute_anomaly_score(zscore=None, velocity_ratio=0.0, entropy_score=0.0)
        assert bot > normal

    def test_clamped_to_one(self):
        score = compute_anomaly_score(zscore=10.0, velocity_ratio=2.0, entropy_score=0.0)
        assert score <= 1.0


# ─── Task difficulty weighting ────────────────────────────────────────────────

class TestTaskDifficulty:
    def test_hard_task_moves_trust_more(self):
        easy = compute_score_update(make_input(difficulty_weight=0.5, quality_score=1.0))
        hard = compute_score_update(make_input(difficulty_weight=3.0, quality_score=1.0))
        assert hard.trust_score > easy.trust_score

    def test_easy_task_moves_trust_less(self):
        normal = compute_score_update(make_input(difficulty_weight=1.0, quality_score=0.0))
        easy   = compute_score_update(make_input(difficulty_weight=0.1, quality_score=0.0))
        # Low quality easy task should hurt less
        assert easy.trust_score >= normal.trust_score


# ─── Score explainability ─────────────────────────────────────────────────────

class TestExplainability:
    def test_all_components_present(self):
        result = compute_score_update(make_input())
        assert result.ewma_component    >= 0
        assert result.accuracy_component >= 0
        assert result.streak_component  >= 0
        assert result.tenure_component  >= 0
        assert result.fraud_multiplier  <= 1.0
        assert result.velocity_penalty  >= 0
        assert "drift_detected" in result.drift_result

    def test_components_sum_correctly(self):
        inp    = make_input(fraud_events_aged=[], tasks_last_hour=0)
        result = compute_score_update(inp)
        # raw = ewma + accuracy + streak + tenure
        # trust = raw * cw * fraud_mult (before max_trust cap)
        raw_sum = (
            result.ewma_component + result.accuracy_component
            + result.streak_component + result.tenure_component
        )
        trust_uncapped = min(100.0, max(0.0, raw_sum))
        expected = round(min(trust_uncapped * result.confidence_weight_val * result.fraud_multiplier,
                             inp.max_trust), 2)
        assert result.trust_score == pytest.approx(expected, abs=1.0)


# ─── Fraud decay ──────────────────────────────────────────────────────────────

class TestFraudDecay:
    def test_recent_event_full_weight(self):
        mult = effective_fraud_multiplier([{"age_days": 0}])
        assert mult == pytest.approx(0.5, rel=0.01)

    def test_halflife_event_half_weight(self):
        mult = effective_fraud_multiplier([{"age_days": 90}])
        # 0.5^(0.5) ≈ 0.707
        assert mult == pytest.approx(0.5 ** 0.5, rel=0.01)

    def test_old_event_minimal_penalty(self):
        mult = effective_fraud_multiplier([{"age_days": 360}])
        # Very old — near 1.0 (tiny weight)
        assert mult > 0.9

    def test_multiple_events_multiplicative(self):
        two_recent = effective_fraud_multiplier([{"age_days": 0}, {"age_days": 0}])
        assert two_recent == pytest.approx(0.25, rel=0.01)   # 0.5^2

    def test_capped_at_three_events(self):
        many = effective_fraud_multiplier([{"age_days": 0}] * 10)
        three = effective_fraud_multiplier([{"age_days": 0}] * 3)
        assert many == pytest.approx(three, rel=0.01)   # both capped at 3


# ─── Mixed population simulation ─────────────────────────────────────────────

class TestMixedPopulationSimulation:
    """
    Simulate 20 honest workers and 5 bots and verify:
    - Honest workers end up with higher trust
    - Bot trust score stays suppressed
    - Fraud penalty prevents bot recovery
    """

    def _simulate_worker(self, quality_scores, fraud_events=0):
        state = ScoreUpdateInput(
            quality_score=0.5, task_type="survey",
            current_ewma=0.5, current_total_tasks=0,
            current_accepted=0, current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_7d_n=0, current_acc_7d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            peer_baseline_n=100, max_trust=100.0,
            fraud_events_aged=[{"age_days": 0}] * fraud_events,
        )
        for q in quality_scores:
            state = ScoreUpdateInput(
                quality_score=q,
                task_type="survey",
                was_accepted=q >= 0.5,
                current_ewma=getattr(state, "_last_ewma", 0.5),
                current_total_tasks=getattr(state, "_last_total", 0) + 1,
                current_accepted=getattr(state, "_last_accepted", 0) + (1 if q >= 0.5 else 0),
                current_acc_30d_n=getattr(state, "_last_n", 0) + 1,
                current_acc_30d_sum=getattr(state, "_last_sum", 0) + (1 if q >= 0.5 else 0),
                current_acc_7d_n=0, current_acc_7d_sum=0,
                current_acc_all_n=getattr(state, "_last_n", 0) + 1,
                current_acc_all_sum=getattr(state, "_last_sum", 0) + (1 if q >= 0.5 else 0),
                peer_baseline_n=100,
                max_trust=70.0 if fraud_events > 0 else 100.0,
                fraud_events_aged=[{"age_days": 0}] * fraud_events,
            )
            result = compute_score_update(state)
            state._last_ewma     = result.ewma_quality
            state._last_total    = getattr(state, "current_total_tasks", 0)
            state._last_accepted = getattr(state, "current_accepted", 0)
            state._last_n        = getattr(state, "current_acc_30d_n", 0)
            state._last_sum      = getattr(state, "current_acc_30d_sum", 0)
        return result

    def test_honest_workers_outperform_bots(self):
        honest_scores = [self._simulate_worker([0.85] * 30).trust_score for _ in range(5)]
        bot_scores    = [self._simulate_worker([0.9] * 30, fraud_events=2).trust_score for _ in range(5)]
        assert min(honest_scores) > max(bot_scores)

    def test_bot_capped_at_max_trust(self):
        result = self._simulate_worker([0.95] * 50, fraud_events=1)
        assert result.trust_score <= 70.0  # max_trust ceiling

    def test_fraud_prevents_full_recovery(self):
        cheater = self._simulate_worker([0.9] * 100, fraud_events=1)
        honest  = self._simulate_worker([0.9] * 100, fraud_events=0)
        assert cheater.trust_score < honest.trust_score


# ─── Baseline poisoning adversarial test ─────────────────────────────────────

class TestBaselinePoisoning:
    """
    A single worker spamming the baseline with extreme scores should not
    shift the mean as much as an evenly distributed population.
    """

    def _build_baseline_from_workers(self, scores_by_worker: dict) -> dict:
        """Build a baseline where each worker contributes their scores."""
        baseline = {}
        for worker_id, scores in scores_by_worker.items():
            for i, score in enumerate(scores):
                baseline = welford_update_weighted(
                    baseline, score,
                    worker_contribution=i,
                    baseline_total=sum(len(s) for s in scores_by_worker.values()),
                )
        return baseline

    def test_poisoner_limited_influence(self):
        # 19 honest workers at mean 80
        honest_workers = {f"w{i}": [80.0] * 5 for i in range(19)}
        # 1 attacker with 50 submissions at extreme 20.0
        poisoner = {"attacker": [20.0] * 50}

        honest_baseline   = self._build_baseline_from_workers(honest_workers)
        poisoned_baseline = self._build_baseline_from_workers({**honest_workers, **poisoner})

        # Mean shift should be limited by soft weighting
        shift = abs(poisoned_baseline["mean"] - honest_baseline["mean"])
        # Without protection, a 50-submission attacker would dominate
        # With soft weighting, shift should be <15 points
        assert shift < 15.0, f"Baseline poisoned too much: mean shifted {shift:.1f} points"

    def test_naive_welford_is_more_susceptible(self):
        """Control test: naive welford with no protection IS more susceptible."""
        from app.services.scoring import welford_update as naive_update

        def build_naive(scores_by_worker):
            bl = {}
            for _, scores in scores_by_worker.items():
                for s in scores:
                    bl = naive_update(bl, s)
            return bl

        honest_workers = {f"w{i}": [80.0] * 5 for i in range(19)}
        poisoner       = {"attacker": [20.0] * 50}

        honest_naive   = build_naive(honest_workers)
        poisoned_naive = build_naive({**honest_workers, **poisoner})

        naive_shift    = abs(poisoned_naive["mean"] - honest_naive["mean"])
        # The naive version should be more susceptible (larger shift)
        # This validates that weighted version is an improvement
        assert naive_shift > 10.0

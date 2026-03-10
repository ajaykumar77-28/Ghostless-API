"""
Unit tests for the real scoring engine (services/scoring.py).

Tests cover:
  - EWMA update math
  - Adaptive alpha decay
  - Confidence weight calculation
  - Z-score computation
  - Welford online variance
  - Full trust score assembly
  - Edge cases: new worker, no peer baseline, fraud penalties
"""
import math
import pytest

from app.services.scoring import (
    EWMA_ALPHA_MATURE,
    EWMA_ALPHA_NEW,
    EWMA_ALPHA_DECAY_N,
    CONFIDENCE_FLOOR,
    CONFIDENCE_CEIL,
    CONFIDENCE_FULL_AT_N,
    ZSCORE_ANOMALY_THRESHOLD,
    ScoreUpdateInput,
    adaptive_alpha,
    compute_score_update,
    compute_zscore,
    confidence_weight,
    update_ewma,
    welford_std,
    welford_update,
)


# ─── Welford online variance ──────────────────────────────────────────────────

class TestWelfordUpdate:
    def test_single_value(self):
        state = welford_update({}, 0.8)
        assert state["n"] == 1
        assert state["mean"] == pytest.approx(0.8)
        assert state["m2"] == pytest.approx(0.0)

    def test_two_values_mean(self):
        state = welford_update({}, 0.6)
        state = welford_update(state, 0.8)
        assert state["mean"] == pytest.approx(0.7)
        assert state["n"] == 2

    def test_known_variance(self):
        # [10, 20, 30] → mean=20, var=200/3≈66.67, std≈8.165
        state = {}
        for v in [10, 20, 30]:
            state = welford_update(state, v)
        std = welford_std(state)
        assert std == pytest.approx(math.sqrt(200 / 3), rel=1e-4)

    def test_std_requires_n_ge_2(self):
        state = welford_update({}, 5.0)
        assert welford_std(state) == 0.0

    def test_identical_values_zero_std(self):
        state = {}
        for _ in range(10):
            state = welford_update(state, 0.75)
        assert welford_std(state) == pytest.approx(0.0, abs=1e-9)


# ─── Adaptive alpha ───────────────────────────────────────────────────────────

class TestAdaptiveAlpha:
    def test_new_worker_high_alpha(self):
        assert adaptive_alpha(0) == pytest.approx(EWMA_ALPHA_NEW)

    def test_mature_worker_low_alpha(self):
        assert adaptive_alpha(EWMA_ALPHA_DECAY_N) == pytest.approx(EWMA_ALPHA_MATURE)
        assert adaptive_alpha(9999) == pytest.approx(EWMA_ALPHA_MATURE)

    def test_midpoint_interpolation(self):
        mid   = EWMA_ALPHA_DECAY_N // 2
        alpha = adaptive_alpha(mid)
        expected = (EWMA_ALPHA_NEW + EWMA_ALPHA_MATURE) / 2
        assert alpha == pytest.approx(expected, abs=0.01)

    def test_alpha_strictly_decreasing(self):
        alphas = [adaptive_alpha(n) for n in range(0, EWMA_ALPHA_DECAY_N + 1)]
        for i in range(1, len(alphas)):
            assert alphas[i] <= alphas[i - 1]


# ─── EWMA ─────────────────────────────────────────────────────────────────────

class TestEWMA:
    def test_full_weight_on_first_update(self):
        # Starting from 0.5, single high-quality task
        result = update_ewma(0.5, 1.0, alpha=0.3)
        assert result == pytest.approx(0.5 * 0.7 + 1.0 * 0.3)

    def test_ewma_converges_to_constant_input(self):
        ewma = 0.5
        for _ in range(200):
            ewma = update_ewma(ewma, 0.9, alpha=0.1)
        assert ewma == pytest.approx(0.9, abs=0.01)

    def test_ewma_bounded_zero_to_one(self):
        ewma = 0.5
        for v in [0.0, 1.0, 0.0, 1.0]:
            ewma = update_ewma(ewma, v, alpha=0.5)
        assert 0.0 <= ewma <= 1.0


# ─── Confidence weight ────────────────────────────────────────────────────────

class TestConfidenceWeight:
    def test_floor_at_zero_tasks(self):
        assert confidence_weight(0) == pytest.approx(CONFIDENCE_FLOOR)

    def test_ceiling_at_full_tasks(self):
        assert confidence_weight(CONFIDENCE_FULL_AT_N) == pytest.approx(CONFIDENCE_CEIL)

    def test_ceiling_above_threshold(self):
        assert confidence_weight(CONFIDENCE_FULL_AT_N + 100) == pytest.approx(CONFIDENCE_CEIL)

    def test_monotonically_increasing(self):
        weights = [confidence_weight(n) for n in range(0, CONFIDENCE_FULL_AT_N + 1)]
        for i in range(1, len(weights)):
            assert weights[i] >= weights[i - 1]


# ─── Z-score ──────────────────────────────────────────────────────────────────

class TestZScore:
    def test_none_when_insufficient_samples(self):
        baseline = {"mean": 80.0, "m2": 100.0, "n": 10}  # n < 30
        assert compute_zscore(75.0, baseline) is None

    def test_zero_std_returns_none(self):
        baseline = {"mean": 80.0, "m2": 0.0, "n": 50}  # std = 0
        assert compute_zscore(80.0, baseline) is None

    def test_mean_value_zscore_zero(self):
        baseline = {"mean": 80.0, "m2": 100.0 * 50, "n": 50}
        # std = sqrt(100*50/50) = 10
        z = compute_zscore(80.0, baseline)
        assert z == pytest.approx(0.0, abs=1e-6)

    def test_one_std_away(self):
        n = 50
        std = 10.0
        m2 = std ** 2 * n
        baseline = {"mean": 80.0, "m2": m2, "n": n}
        z = compute_zscore(90.0, baseline)   # 1 std above mean
        assert z == pytest.approx(1.0, rel=0.01)

    def test_anomaly_threshold(self):
        n   = 50
        std = 10.0
        m2  = std ** 2 * n
        bl  = {"mean": 80.0, "m2": m2, "n": n}
        # 3 std above mean → should trigger anomaly
        z   = compute_zscore(80.0 + 3 * std, bl)
        assert z is not None and abs(z) > ZSCORE_ANOMALY_THRESHOLD


# ─── Full trust score assembly ────────────────────────────────────────────────

class TestComputeScoreUpdate:

    def _make_input(self, **kwargs) -> ScoreUpdateInput:
        defaults = dict(
            quality_score=0.8,
            task_type="image_label",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=5,
            days_since_joined=30.0,
            current_ewma=0.5,
            current_total_tasks=5,
            current_accepted=4,
            current_acc_7d_n=5, current_acc_7d_sum=4,
            current_acc_30d_n=5, current_acc_30d_sum=4,
            current_acc_all_n=5, current_acc_all_sum=4,
            task_baselines={},
            current_zscore_flagged=0,
        )
        defaults.update(kwargs)
        return ScoreUpdateInput(**defaults)

    def test_trust_score_in_range(self):
        result = compute_score_update(self._make_input())
        assert 0.0 <= result.trust_score <= 100.0

    def test_high_quality_increases_ewma(self):
        inp    = self._make_input(quality_score=1.0, current_ewma=0.5)
        result = compute_score_update(inp)
        assert result.ewma_quality > 0.5

    def test_low_quality_decreases_ewma(self):
        inp    = self._make_input(quality_score=0.0, current_ewma=0.8)
        result = compute_score_update(inp)
        assert result.ewma_quality < 0.8

    def test_fraud_events_penalize_score(self):
        clean = compute_score_update(self._make_input(fraud_event_count=0))
        dirty = compute_score_update(self._make_input(fraud_event_count=5))
        assert clean.trust_score > dirty.trust_score

    def test_new_worker_low_confidence(self):
        inp    = self._make_input(current_accepted=0, current_total_tasks=0)
        result = compute_score_update(inp)
        assert result.confidence_weight_val <= 0.5

    def test_veteran_worker_high_confidence(self):
        inp    = self._make_input(current_accepted=50, current_acc_all_n=50, current_acc_all_sum=45)
        result = compute_score_update(inp)
        assert result.confidence_weight_val == pytest.approx(1.0)

    def test_accuracy_windows_computed(self):
        inp    = self._make_input(was_accepted=True)
        result = compute_score_update(inp)
        assert result.accuracy_7d is not None
        assert result.accuracy_30d is not None
        assert result.accuracy_all is not None

    def test_pending_tasks_excluded_from_accuracy(self):
        inp    = self._make_input(was_accepted=None)
        result = compute_score_update(inp)
        # should not increment accuracy counters for pending tasks
        assert result.accuracy_7d == pytest.approx(4 / 5 * 100, abs=0.1)  # unchanged

    def test_task_baselines_updated(self):
        inp    = self._make_input(task_type="survey", quality_score=0.75)
        result = compute_score_update(inp)
        assert "survey" in result.task_baselines
        assert result.task_baselines["survey"]["n"] == 1

    def test_streak_improves_score(self):
        low_streak  = compute_score_update(self._make_input(streak_days=0))
        high_streak = compute_score_update(self._make_input(streak_days=30))
        assert high_streak.trust_score > low_streak.trust_score

    def test_score_clamped_to_100(self):
        # Perfect everything
        inp = self._make_input(
            quality_score=1.0, current_ewma=1.0, was_accepted=True,
            fraud_event_count=0, streak_days=30, days_since_joined=365,
            current_accepted=50, current_acc_all_n=50, current_acc_all_sum=50,
        )
        result = compute_score_update(inp)
        assert result.trust_score <= 100.0

    def test_score_clamped_to_zero(self):
        inp = self._make_input(
            quality_score=0.0, current_ewma=0.0, was_accepted=False,
            fraud_event_count=10, streak_days=0, days_since_joined=0,
            current_acc_all_n=20, current_acc_all_sum=0,
        )
        result = compute_score_update(inp)
        assert result.trust_score >= 0.0

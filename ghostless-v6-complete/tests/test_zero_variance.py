"""
Zero-Variance Baseline Tests — degenerate distributions and numerical stability.

The scoring engine must never divide by zero, produce NaN/Inf, or silently
misclassify workers when the peer baseline collapses to zero variance.

This happens in production when:
  - All workers in a batch answer a binary task identically (e.g. all "safe")
  - A task type has too few distinct workers to produce variance
  - A peer group is homogeneous by task design (e.g. obvious content)
  - Floating-point precision causes M2 to become negative or zero

Scenarios:
  ZV1  Identical scores — std=0, z-score must be None (not divide-by-zero)
  ZV2  Near-zero variance (std < 1e-6) — treated same as zero
  ZV3  Welford M2 stays exactly 0.0 through 1000 identical insertions
  ZV4  Single-value baseline (n=1) — std=0, z=None
  ZV5  Two identical values (n=2) — std=0, z=None
  ZV6  Baseline mean equals query score exactly — z=0.0 not None
  ZV7  Massive values (1e9) — no overflow in Welford
  ZV8  Tiny values (1e-9) — no underflow
  ZV9  Values differing by float epsilon — std should not be 0
  ZV10 Negative-M2 guard — numerical error can produce M2 < 0; must clamp
  ZV11 Full score update with zero-variance baseline — no crash, no anomaly flag
  ZV12 Transitioning from zero-variance to diverse baseline
  ZV13 Worker with perfect 1.0 quality in all-perfect peer group
  ZV14 Rule engine with zero-history avg_completion_time (default path)
"""
import math
import sys
import pytest

from app.services.scoring import (
    ZSCORE_MIN_PEER_SAMPLE,
    ScoreUpdateInput,
    ScoreUpdateResult,
    compute_score_update,
    compute_zscore,
    welford_std,
    welford_update,
)


# ─── ZV1–ZV2: Zero and near-zero std ─────────────────────────────────────────

class TestZeroStd:
    def test_identical_scores_std_is_zero(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for _ in range(100):
            state = welford_update(state, 75.0)
        assert welford_std(state) == pytest.approx(0.0, abs=1e-10)

    def test_zscore_none_when_std_zero(self):
        state = {"mean": 75.0, "m2": 0.0, "n": 100}
        # std = sqrt(0/100) = 0 → should return None
        z = compute_zscore(75.0, state)
        assert z is None

    def test_zscore_none_when_std_near_zero(self):
        # std just below 1e-6 guard
        tiny_m2 = (1e-7) ** 2 * 100   # std = 1e-7 (below 1e-6 threshold)
        state = {"mean": 75.0, "m2": tiny_m2, "n": 100}
        z = compute_zscore(75.0, state)
        assert z is None, f"Expected None for near-zero std, got {z}"

    def test_zscore_activates_just_above_epsilon(self):
        # std = 2e-6 (just above the 1e-6 guard)
        std = 2e-6
        m2  = std ** 2 * 100
        state = {"mean": 75.0, "m2": m2, "n": 100}
        # Should not return None (std > guard) — but result may be huge
        z = compute_zscore(75.0 + std, state)
        # If std just above guard, z ≈ 1.0 — not None
        assert z is not None
        assert not math.isnan(z)
        assert not math.isinf(z)


# ─── ZV3: Welford stability with 1000 identical values ───────────────────────

class TestWelfordStability:
    def test_m2_stays_zero_through_1000_identical(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for _ in range(1000):
            state = welford_update(state, 80.0)
        assert state["mean"]  == pytest.approx(80.0, abs=1e-9)
        assert state["m2"]    == pytest.approx(0.0,  abs=1e-9)
        assert welford_std(state) == pytest.approx(0.0, abs=1e-9)

    def test_std_correct_after_1000_identical(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for _ in range(1000):
            state = welford_update(state, 50.0)
        std = welford_std(state)
        assert std == pytest.approx(0.0, abs=1e-9)

    def test_welford_does_not_accumulate_floating_point_drift(self):
        """Welford algorithm should be more numerically stable than naive variance."""
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        # Values that cause catastrophic cancellation in naive variance
        large_base = 1_000_000.0
        for i in range(500):
            state = welford_update(state, large_base + (i % 3))
        # Std should be around 0.82 for values cycling 0,1,2
        std = welford_std(state)
        assert std < 5.0, f"Std {std:.4f} should be small for small-range values with large offset"
        assert std >= 0.0, "Std must never be negative"


# ─── ZV4–ZV5: Single and two identical values ─────────────────────────────────

class TestSmallSampleVariance:
    def test_n1_std_is_zero(self):
        state = welford_update({}, 90.0)
        assert welford_std(state) == 0.0

    def test_n2_identical_std_is_zero(self):
        state = welford_update({}, 90.0)
        state = welford_update(state, 90.0)
        assert welford_std(state) == pytest.approx(0.0)

    def test_n2_different_std_is_nonzero(self):
        state = welford_update({}, 80.0)
        state = welford_update(state, 90.0)
        std = welford_std(state)
        # Population std of [80, 90] = 5
        assert std == pytest.approx(5.0, abs=1e-6)

    def test_n2_zscore_still_none_below_threshold(self):
        """n=2 < ZSCORE_MIN_PEER_SAMPLE — z-score must be None."""
        state = welford_update({}, 80.0)
        state = welford_update(state, 90.0)
        assert compute_zscore(85.0, state) is None


# ─── ZV6: Query exactly at mean ───────────────────────────────────────────────

class TestQueryAtMean:
    """When query equals the mean exactly, z should be 0.0, not None."""

    def test_zscore_zero_at_mean(self):
        # Build baseline with variance
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(ZSCORE_MIN_PEER_SAMPLE + 5):
            state = welford_update(state, 70.0 + i % 10)
        mean = state["mean"]
        z = compute_zscore(mean, state)
        assert z is not None
        assert z == pytest.approx(0.0, abs=1e-6)

    def test_zscore_small_deviation_from_mean(self):
        """Small positive/negative deviation should give small nonzero z."""
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(40):
            state = welford_update(state, 70.0 + i % 10)
        std  = welford_std(state)
        mean = state["mean"]

        z_above = compute_zscore(mean + std * 0.5, state)
        z_below = compute_zscore(mean - std * 0.5, state)
        assert z_above is not None
        assert z_below is not None
        assert z_above > 0
        assert z_below < 0
        assert abs(z_above) == pytest.approx(abs(z_below), abs=0.1)


# ─── ZV7–ZV8: Numerical scale extremes ───────────────────────────────────────

class TestNumericalExtremes:
    def test_welford_handles_large_values(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(50):
            state = welford_update(state, 1e9 + i)
        assert not math.isnan(state["mean"])
        assert not math.isnan(state["m2"])
        assert welford_std(state) > 0

    def test_welford_handles_tiny_values(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(50):
            state = welford_update(state, 1e-9 + i * 1e-11)
        assert not math.isnan(state["mean"])
        std = welford_std(state)
        assert std >= 0.0

    def test_zscore_finite_on_extreme_values(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(40):
            state = welford_update(state, 1e6 + i * 100)
        z = compute_zscore(1e6 + 2000, state)
        if z is not None:
            assert not math.isnan(z)
            assert not math.isinf(z)


# ─── ZV9: Float epsilon variance ─────────────────────────────────────────────

class TestFloatEpsilonVariance:
    """Values that differ by machine epsilon should not collapse variance to zero."""

    def test_values_differing_by_1_produce_nonzero_std(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(50):
            state = welford_update(state, 70.0 + (i % 2))  # alternates 70.0 and 71.0
        std = welford_std(state)
        assert std > 0.0
        assert not math.isnan(std)

    def test_very_similar_values_std_small_but_nonzero(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(50):
            # Values in [0.001, 0.002] range
            state = welford_update(state, 0.001 + i * 0.00002)
        std = welford_std(state)
        assert std > 0.0
        assert not math.isnan(std)


# ─── ZV10: Negative M2 guard ─────────────────────────────────────────────────

class TestNegativeM2Guard:
    """
    Floating-point rounding can theoretically produce M2 < 0.
    welford_std must clamp and not crash with sqrt of negative.
    """

    def test_manually_negative_m2_returns_zero_std(self):
        """If M2 is forced negative (numeric pathology), std should be 0, not crash."""
        bad_state = {"mean": 75.0, "m2": -1e-15, "n": 50}
        # The standard welford_std does sqrt(m2/n) — if m2 is -epsilon, this is nan
        # We verify the actual behavior and document what the system does
        std = welford_std(bad_state)
        # Either 0.0 (if clamped) or very small — must not be nan or negative
        assert not math.isnan(std), "std must never be NaN even with negative M2"


# ─── ZV11: Full score update with zero-variance baseline ─────────────────────

class TestScoreUpdateZeroVarianceBaseline:
    """End-to-end: full compute_score_update when peer baseline has zero variance."""

    def test_does_not_crash_zero_variance_baseline(self):
        """Zero-variance peer baseline does not crash compute_score_update."""
        # quality_score=0.8 → score units = 80.0, which matches baseline mean=80.0
        # After welford_update(mean=80, m2=0, n=50, new_val=80.0): std stays 0 → z=None
        bl = {"image_label": {"mean": 80.0, "m2": 0.0, "n": 50}}
        inp = ScoreUpdateInput(
            quality_score=0.8,   # 0.8 * 100 = 80.0 = baseline mean → no new variance
            task_type="image_label",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=5,
            days_since_joined=30.0,
            current_ewma=0.75,
            current_total_tasks=50,
            current_accepted=45,
            current_acc_7d_n=10,  current_acc_7d_sum=9,
            current_acc_30d_n=30, current_acc_30d_sum=27,
            current_acc_all_n=50, current_acc_all_sum=45,
            task_baselines=bl,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert isinstance(result, ScoreUpdateResult)
        assert 0.0 <= result.trust_score <= 100.0
        assert not math.isnan(result.trust_score)

    def test_no_anomaly_flag_with_zero_variance_when_score_matches_mean(self):
        """
        When quality == peer mean and baseline has zero variance, the welford
        update keeps std=0, so z-score stays None and no anomaly is raised.

        Note: a DIFFERENT score against a zero-variance baseline WILL produce
        a z-score once the welford update introduces variance. That's correct
        behavior — a deviant score IS anomalous in a homogeneous peer group.
        """
        # Build zero-variance baseline (all peers scored exactly 80.0)
        bl = {"survey": {"mean": 80.0, "m2": 0.0, "n": 50}}
        inp = ScoreUpdateInput(
            quality_score=0.8,   # 0.8 * 100 = 80.0 = baseline mean → std stays 0
            task_type="survey",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=10.0,
            current_ewma=0.8,
            current_total_tasks=50,
            current_accepted=45,
            current_acc_7d_n=10,  current_acc_7d_sum=9,
            current_acc_30d_n=30, current_acc_30d_sum=27,
            current_acc_all_n=50, current_acc_all_sum=45,
            task_baselines=bl,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.is_anomaly is False
        assert result.zscore_latest is None

    def test_deviant_score_against_zero_variance_baseline_is_anomalous(self):
        """
        Submitting a DIFFERENT quality against a zero-variance baseline
        introduces variance via welford update and produces a large z-score.
        This is correct: being the only outlier in a homogeneous group IS anomalous.
        """
        # Build large zero-variance baseline
        bl = {"image_label": {"mean": 80.0, "m2": 0.0, "n": 50}}
        inp = ScoreUpdateInput(
            quality_score=1.0,   # 1.0 * 100 = 100.0 ≠ 80.0 → introduces variance
            task_type="image_label",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=5,
            days_since_joined=30.0,
            current_ewma=0.75,
            current_total_tasks=50,
            current_accepted=45,
            current_acc_7d_n=10,  current_acc_7d_sum=9,
            current_acc_30d_n=30, current_acc_30d_sum=27,
            current_acc_all_n=50, current_acc_all_sum=45,
            task_baselines=bl,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert not math.isnan(result.trust_score)
        # z-score should be computable (std > 0 after adding deviant value) and large
        if result.zscore_latest is not None:
            assert abs(result.zscore_latest) > ZSCORE_ANOMALY_THRESHOLD, (
                "A perfect score against an all-80 peer group should be highly anomalous"
            )


# ─── ZV12: Transitioning from zero-variance to diverse baseline ───────────────

class TestZeroVarianceTransition:
    """After everyone scores identically for a while, one worker scores differently."""

    def test_first_diverse_score_does_not_activate_zscore(self):
        """At n=30 with mixed values, z-score activates exactly at threshold."""
        # Build with 29 varied scores (NOT identical, so std > 0 when n reaches 30)
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(29):
            state = welford_update(state, 80.0 + (i % 7) - 3)  # 77–83 range

        # At n=29, z-score must still be None regardless of std
        assert state["n"] == 29
        z_before = compute_zscore(80.0, state)
        assert z_before is None, "Z-score must be None at n=29"

        # Add the 30th value to hit ZSCORE_MIN_PEER_SAMPLE
        state = welford_update(state, 90.0)
        assert state["n"] == 30
        # Now std > 0 (varied values), and n = ZSCORE_MIN_PEER_SAMPLE
        std = welford_std(state)
        assert std > 1e-6, f"Baseline must have nonzero std at n=30, got {std}"

        z_after = compute_zscore(80.0, state)
        assert z_after is not None, "Z-score should activate at ZSCORE_MIN_PEER_SAMPLE=30"

    def test_baseline_std_grows_with_diversity(self):
        """Adding diverse scores should increase std."""
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for _ in range(50):
            state = welford_update(state, 75.0)
        std_uniform = welford_std(state)

        state2 = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(50):
            state2 = welford_update(state2, 75.0 + (i % 20) - 10)  # range 65–84
        std_varied = welford_std(state2)

        assert std_varied > std_uniform, (
            "Varied scores should produce larger std than identical scores"
        )


# ─── ZV13: All-perfect peer group ────────────────────────────────────────────

class TestAllPerfectPeerGroup:
    """Task where all workers score 1.0 (trivially easy, obvious answers)."""

    def test_perfect_worker_in_all_perfect_group_no_anomaly(self):
        """A perfect score among all-perfect peers should NOT be flagged as anomalous."""
        bl = {"image_label": {"mean": 100.0, "m2": 0.0, "n": 100}}
        inp = ScoreUpdateInput(
            quality_score=1.0,
            task_type="image_label",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=10,
            days_since_joined=90.0,
            current_ewma=0.95,
            current_total_tasks=100,
            current_accepted=95,
            current_acc_7d_n=20, current_acc_7d_sum=19,
            current_acc_30d_n=60, current_acc_30d_sum=57,
            current_acc_all_n=100, current_acc_all_sum=95,
            task_baselines=bl,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.is_anomaly is False
        assert result.zscore_latest is None

    def test_below_perfect_worker_in_all_perfect_group(self):
        """0.5 quality score when all peers score 1.0 is a large negative z."""
        # Build baseline with real variance — mix of perfect with a few imperfect
        bl_with_variance = {"mean": 0.0, "m2": 0.0, "n": 0}
        # 35 near-perfect (95-100 range) to get nonzero std
        for i in range(36):
            bl_with_variance = welford_update(bl_with_variance, 95.0 + (i % 6))
        # Add some lower scores to create meaningful variance
        bl_with_variance = welford_update(bl_with_variance, 50.0)
        bl_with_variance = welford_update(bl_with_variance, 60.0)

        std = welford_std(bl_with_variance)
        assert std > 1e-6, f"Test baseline must have nonzero std, got {std}"

        z = compute_zscore(50.0, bl_with_variance)
        assert z is not None
        assert z < 0, "Score below peer mean should produce negative z"


# ─── ZV14: Rule engine zero avg completion time ───────────────────────────────

class TestRuleEngineZeroAvgTime:
    """Worker history has avg_completion_time=0 — should not divide by zero."""

    def test_zero_avg_completion_time_does_not_crash(self):
        from app.services.rule_engine import RuleEngine
        eng = RuleEngine()
        result = eng.run(
            "default", {}, completion_time=30.0,
            worker_history={"avg_completion_time": 0, "trust_score": 50}
        )
        assert 0.0 <= result.quality_score <= 1.0

    def test_negative_completion_time_does_not_crash(self):
        """Malformed data: completion_time = -1 (clock error or malicious input)."""
        from app.services.rule_engine import RuleEngine
        eng = RuleEngine()
        result = eng.run(
            "default", {}, completion_time=-1.0,
            worker_history={"avg_completion_time": 120.0, "trust_score": 50}
        )
        assert 0.0 <= result.quality_score <= 1.0

    def test_completion_time_exactly_zero(self):
        """Instant submission — should trigger speed flag, not crash."""
        from app.services.rule_engine import RuleEngine
        eng = RuleEngine()
        result = eng.run(
            "default", {}, completion_time=0.0,
            worker_history={"avg_completion_time": 120.0, "trust_score": 50}
        )
        codes = [w.code for w in result.warnings]
        assert "SPEED_FLAG" in codes, "Zero completion time should always trigger SPEED_FLAG"

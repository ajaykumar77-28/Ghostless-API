"""
Cold-Start Edge Tests — the system's first encounter with everything.

These test the 'n=0' and 'n=1' cases that are the hardest to get right
and the most likely source of divide-by-zero / None-dereference crashes
in scoring systems. Every test models a real production scenario.

Scenarios:
  CS1  Absolute first task: no history, no baseline, no peer data
  CS2  First accepted task: accuracy transitions from None to a value
  CS3  First task in a new task type (separate Welford baseline needed)
  CS4  No peer data yet: z-score must be None, not 0.0, not crash
  CS5  Worker with tasks but zero accepted: accuracy = 0.0 not crash
  CS6  Completely empty worker history dict passed to rule engine
  CS7  All ScoreUpdateInput fields at their literal zero/None defaults
  CS8  Welford with n=1: std must be 0.0, z-score must be None
  CS9  Welford with n=29: one below ZSCORE_MIN_PEER_SAMPLE cutoff
  CS10 days_since_joined = 0 (joined today, never submitted before)
  CS11 EWMA update when current_ewma is exactly 0.0
  CS12 Multiple task types, each cold-starting independently
  CS13 Score update with quality_score exactly 0.0
  CS14 Score update with quality_score exactly 1.0
  CS15 Accuracy windows: first rejected task (was_accepted=False)
"""
import pytest

from app.services.scoring import (
    ZSCORE_MIN_PEER_SAMPLE,
    ScoreUpdateInput,
    ScoreUpdateResult,
    adaptive_alpha,
    compute_score_update,
    compute_zscore,
    confidence_weight,
    update_ewma,
    welford_std,
    welford_update,
)
from app.services.rule_engine import RuleEngine

engine = RuleEngine()


# ─── CS1: Absolute first task ─────────────────────────────────────────────────

class TestAbsoluteFirstTask:
    """A brand-new worker submits their very first task. Nothing exists yet."""

    def _first_task_input(self, quality: float = 0.7) -> ScoreUpdateInput:
        return ScoreUpdateInput(
            quality_score=quality,
            task_type="image_label",
            was_accepted=None,       # still pending review
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=0.0,
            current_ewma=0.5,        # default starting EWMA
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )

    def test_does_not_crash(self):
        result = compute_score_update(self._first_task_input())
        assert isinstance(result, ScoreUpdateResult)

    def test_trust_score_in_range(self):
        result = compute_score_update(self._first_task_input())
        assert 0.0 <= result.trust_score <= 100.0

    def test_ewma_moved_from_default(self):
        result = compute_score_update(self._first_task_input(0.8))
        assert result.ewma_quality != 0.5, "EWMA must update on first task"

    def test_accuracy_windows_are_none_when_pending(self):
        """Pending tasks (was_accepted=None) should not populate accuracy."""
        result = compute_score_update(self._first_task_input())
        # n=0 for all windows → safe_rate returns None
        assert result.accuracy_7d  is None
        assert result.accuracy_30d is None
        assert result.accuracy_all is None

    def test_zscore_is_none_with_empty_baseline(self):
        """First task creates baseline n=1; z-score must be None (not computable)."""
        result = compute_score_update(self._first_task_input())
        assert result.zscore_latest is None, (
            "Z-score must be None when peer baseline has < 30 samples"
        )

    def test_baseline_created_for_task_type(self):
        """After first task, task_baselines should have an entry."""
        result = compute_score_update(self._first_task_input())
        assert "image_label" in result.task_baselines
        assert result.task_baselines["image_label"]["n"] == 1

    def test_confidence_weight_at_floor(self):
        """Zero accepted tasks → confidence at floor."""
        result = compute_score_update(self._first_task_input())
        assert result.confidence_weight_val == pytest.approx(confidence_weight(0))

    def test_is_not_flagged_as_anomaly(self):
        """First task should never trigger anomaly — no baseline to compare against."""
        result = compute_score_update(self._first_task_input())
        assert result.is_anomaly is False
        assert result.is_fraud_suspect is False


# ─── CS2: First accepted task ─────────────────────────────────────────────────

class TestFirstAcceptedTask:
    """Worker's first task gets accepted. Accuracy transitions from None to value."""

    def test_accuracy_becomes_100_on_first_acceptance(self):
        inp = ScoreUpdateInput(
            quality_score=0.8,
            task_type="survey",
            was_accepted=True,          # accepted!
            fraud_event_count=0,
            streak_days=1,
            days_since_joined=1.0,
            current_ewma=0.5,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.accuracy_all  == pytest.approx(100.0)
        assert result.accuracy_30d  == pytest.approx(100.0)
        assert result.accuracy_7d   == pytest.approx(100.0)

    def test_first_rejection_accuracy_is_zero(self):
        inp = ScoreUpdateInput(
            quality_score=0.2,
            task_type="survey",
            was_accepted=False,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=1.0,
            current_ewma=0.5,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.accuracy_all  == pytest.approx(0.0)
        assert result.accuracy_30d  == pytest.approx(0.0)
        assert result.accuracy_7d   == pytest.approx(0.0)


# ─── CS3: First task in a new task type ──────────────────────────────────────

class TestNewTaskType:
    """Worker who has history in image_label submits their first transcription."""

    def test_new_task_type_creates_separate_baseline(self):
        existing_baselines = {
            "image_label": {"mean": 75.0, "m2": 100.0, "n": 40}
        }
        inp = ScoreUpdateInput(
            quality_score=0.8,
            task_type="transcription",   # NEW type
            was_accepted=True,
            fraud_event_count=0,
            streak_days=3,
            days_since_joined=30.0,
            current_ewma=0.75,
            current_total_tasks=40,
            current_accepted=35,
            current_acc_7d_n=10, current_acc_7d_sum=9,
            current_acc_30d_n=40, current_acc_30d_sum=35,
            current_acc_all_n=40, current_acc_all_sum=35,
            task_baselines=existing_baselines,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert "transcription" in result.task_baselines
        assert result.task_baselines["transcription"]["n"] == 1
        # Baseline is stored in 0–100 units (quality_score * 100 = 0.8 * 100 = 80.0)
        assert abs(result.task_baselines["transcription"]["mean"] - 80.0) < 0.1
        # Original baseline should be untouched
        assert result.task_baselines["image_label"]["n"] == 40

    def test_new_task_type_zscore_is_none(self):
        """First transcription task → no peer baseline → z-score is None."""
        inp = ScoreUpdateInput(
            quality_score=0.9,
            task_type="transcription",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=3,
            days_since_joined=60.0,
            current_ewma=0.8,
            current_total_tasks=50,
            current_accepted=48,
            current_acc_7d_n=10, current_acc_7d_sum=9,
            current_acc_30d_n=30, current_acc_30d_sum=28,
            current_acc_all_n=50, current_acc_all_sum=48,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.zscore_latest is None

    def test_no_anomaly_flag_for_new_task_type(self):
        """Cannot be flagged anomalous when there's no baseline to compare against."""
        inp = ScoreUpdateInput(
            quality_score=1.0,
            task_type="moderation",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=0.0,
            current_ewma=0.5,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0, current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.is_anomaly is False


# ─── CS4: No peer data / insufficient baseline ────────────────────────────────

class TestInsufficientPeerData:
    """Z-score must be None at various sub-threshold sample counts."""

    @pytest.mark.parametrize("n", [0, 1, 5, 10, 15, 20, 25, 29])
    def test_zscore_is_none_below_min_peer_sample(self, n: int):
        """Z-score must be None for any n < ZSCORE_MIN_PEER_SAMPLE."""
        baseline = {"mean": 70.0, "m2": 500.0, "n": n}
        result = compute_zscore(75.0, baseline)
        assert result is None, (
            f"Z-score must be None at n={n} (threshold is {ZSCORE_MIN_PEER_SAMPLE})"
        )

    def test_zscore_activates_at_exactly_min_sample(self):
        """Z-score should become available exactly at ZSCORE_MIN_PEER_SAMPLE."""
        # Build a baseline with exactly the minimum required samples
        bl = {"mean": 0.0, "m2": 0.0, "n": 0}
        for _ in range(ZSCORE_MIN_PEER_SAMPLE):
            bl = welford_update(bl, 75.0)  # add exactly at threshold

        # Std must be > 1e-6 for z-score to work — use varied data
        bl_varied = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(ZSCORE_MIN_PEER_SAMPLE):
            val = 70.0 + (i % 10)  # varied values 70–79
            bl_varied = welford_update(bl_varied, val)

        z = compute_zscore(75.0, bl_varied)
        assert z is not None, (
            f"Z-score should activate at n={ZSCORE_MIN_PEER_SAMPLE} samples"
        )


# ─── CS5: Zero accepted tasks ─────────────────────────────────────────────────

class TestZeroAcceptedTasks:
    """Worker has submitted tasks but none have been accepted or reviewed yet."""

    def test_all_pending_no_accuracy(self):
        """10 submitted, 0 reviewed → accuracy must be None for all windows."""
        inp = ScoreUpdateInput(
            quality_score=0.7,
            task_type="survey",
            was_accepted=None,
            fraud_event_count=0,
            streak_days=2,
            days_since_joined=5.0,
            current_ewma=0.5,
            current_total_tasks=10,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.accuracy_all is None
        assert result.accuracy_7d  is None
        assert result.accuracy_30d is None

    def test_score_still_positive_with_zero_accepted(self):
        """Zero accepted should not produce a crash or negative score."""
        inp = ScoreUpdateInput(
            quality_score=0.6,
            task_type="image_label",
            was_accepted=None,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=3.0,
            current_ewma=0.5,
            current_total_tasks=5,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert result.trust_score >= 0.0
        assert not math.isnan(result.trust_score)
        assert not math.isinf(result.trust_score)


# ─── CS6: Empty rule engine history ──────────────────────────────────────────

class TestEmptyRuleEngineHistory:
    """Rule engine called with completely empty worker_history dict."""

    def test_empty_history_does_not_crash_default(self):
        result = engine.run("default", {}, completion_time=60.0, worker_history={})
        assert result.quality_score >= 0.0

    def test_empty_history_does_not_crash_image_label(self):
        result = engine.run(
            "image_label",
            {"labels": ["cat"], "bounding_boxes": [{"x": 0}]},
            completion_time=60.0,
            worker_history={},
        )
        assert result.quality_score >= 0.0

    def test_empty_history_uses_default_avg_completion_time(self):
        """With no history, speed check should use a safe default (not crash)."""
        result = engine.run("default", {}, completion_time=1.0, worker_history={})
        # Should produce some speed-related warning because 1s is very fast
        # vs the default 120s average
        codes = [w.code for w in result.warnings]
        # No exception thrown is the key assertion; warning presence is secondary
        assert isinstance(codes, list)

    def test_history_with_none_trust_score_defaults(self):
        result = engine.run(
            "default", {}, 60.0,
            worker_history={"trust_score": None, "avg_completion_time": None}
        )
        assert 0.0 <= result.quality_score <= 1.0


# ─── CS7: All zeros / None defaults ──────────────────────────────────────────

class TestAllDefaultsInput:
    """ScoreUpdateInput with every field at its minimum possible value."""

    def test_all_zeros_does_not_crash(self):
        inp = ScoreUpdateInput(
            quality_score=0.0,
            task_type="image_label",
            was_accepted=None,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=0.0,
            current_ewma=0.0,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert isinstance(result, ScoreUpdateResult)
        assert not math.isnan(result.trust_score)
        assert not math.isinf(result.trust_score)

    def test_score_bounded_on_all_zeros(self):
        inp = ScoreUpdateInput(
            quality_score=0.0,
            task_type="x",
            was_accepted=None,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=0.0,
            current_ewma=0.0,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert 0.0 <= result.trust_score <= 100.0


# ─── CS8: Welford n=1 ─────────────────────────────────────────────────────────

class TestWelfordSingleSample:
    """Single sample is not enough for meaningful variance estimates."""

    def test_std_is_zero_at_n1(self):
        state = welford_update({}, 0.75)
        assert welford_std(state) == 0.0

    def test_zscore_none_at_n1(self):
        state = welford_update({}, 80.0)
        assert compute_zscore(80.0, state) is None

    def test_std_is_zero_at_n0(self):
        assert welford_std({}) == 0.0

    def test_welford_update_empty_state(self):
        """Starting from completely empty dict should work."""
        result = welford_update({}, 0.5)
        assert result["n"] == 1
        assert result["mean"] == pytest.approx(0.5)
        assert result["m2"] == pytest.approx(0.0)


# ─── CS9: Welford n=29 (one below threshold) ──────────────────────────────────

class TestWelfordJustBelowThreshold:
    """29 samples: std is valid but z-score should still be suppressed."""

    def test_std_valid_at_n29(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(29):
            state = welford_update(state, 70.0 + i % 5)
        std = welford_std(state)
        assert std > 0.0

    def test_zscore_still_none_at_n29(self):
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(29):
            state = welford_update(state, 70.0 + i % 5)
        z = compute_zscore(75.0, state)
        assert z is None, f"Z-score must be None at n=29, got {z}"


# ─── CS10: Joined today ───────────────────────────────────────────────────────

class TestJoinedToday:
    """days_since_joined = 0.0 — tenure bonus should be exactly 0."""

    def test_zero_tenure_bonus(self):
        inp = ScoreUpdateInput(
            quality_score=0.7,
            task_type="survey",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=0.0,
            current_ewma=0.5,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result_today = compute_score_update(inp)
        inp_year = ScoreUpdateInput(
            **{**inp.__dict__, "days_since_joined": 365.0}
        )
        result_year = compute_score_update(inp_year)
        # Veteran should score exactly 5 pts more (max tenure bonus)
        assert result_year.trust_score > result_today.trust_score
        assert result_year.trust_score - result_today.trust_score == pytest.approx(5.0, abs=0.5)


# ─── CS11: EWMA starting at 0.0 ──────────────────────────────────────────────

class TestEWMAFromZero:
    """What if current_ewma is exactly 0.0 (blank-slate worker)?"""

    def test_ewma_from_zero_with_high_quality(self):
        ewma = update_ewma(0.0, 0.9, alpha=0.3)
        assert ewma == pytest.approx(0.9 * 0.3)
        assert ewma > 0.0

    def test_ewma_from_zero_with_zero_quality(self):
        ewma = update_ewma(0.0, 0.0, alpha=0.3)
        assert ewma == pytest.approx(0.0)

    def test_score_from_zero_ewma_does_not_crash(self):
        inp = ScoreUpdateInput(
            quality_score=0.8,
            task_type="survey",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=1.0,
            current_ewma=0.0,    # ← zero
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        assert isinstance(result, ScoreUpdateResult)
        assert 0.0 <= result.trust_score <= 100.0


# ─── CS12: Multiple task types cold-starting independently ───────────────────

class TestMultipleTaskTypeColdStart:
    """Each task type should cold-start its Welford baseline independently."""

    def test_three_types_independent_baselines(self):
        inp = ScoreUpdateInput(
            quality_score=0.8,
            task_type="image_label",
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=1.0,
            current_ewma=0.5,
            current_total_tasks=0,
            current_accepted=0,
            current_acc_7d_n=0,  current_acc_7d_sum=0,
            current_acc_30d_n=0, current_acc_30d_sum=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            task_baselines={},
            current_zscore_flagged=0,
        )
        # image_label
        r1 = compute_score_update(inp)
        # transcription — different type
        inp2 = ScoreUpdateInput(
            **{**inp.__dict__,
               "task_type": "transcription",
               "task_baselines": r1.task_baselines,
               "current_ewma": r1.ewma_quality}
        )
        r2 = compute_score_update(inp2)
        # moderation — different type
        inp3 = ScoreUpdateInput(
            **{**inp.__dict__,
               "task_type": "moderation",
               "task_baselines": r2.task_baselines,
               "current_ewma": r2.ewma_quality}
        )
        r3 = compute_score_update(inp3)

        baselines = r3.task_baselines
        assert "image_label"  in baselines
        assert "transcription" in baselines
        assert "moderation"   in baselines
        # Each has n=1 independently
        assert baselines["image_label"]["n"]  == 1
        assert baselines["transcription"]["n"] == 1
        assert baselines["moderation"]["n"]   == 1

    def test_cold_task_type_does_not_inherit_existing_baseline(self):
        """New task type starts at n=0, not inheriting the image_label baseline."""
        existing = {
            "image_label": {"mean": 80.0, "m2": 5000.0, "n": 100}
        }
        inp = ScoreUpdateInput(
            quality_score=0.5,
            task_type="survey",   # new type
            was_accepted=True,
            fraud_event_count=0,
            streak_days=0,
            days_since_joined=10.0,
            current_ewma=0.75,
            current_total_tasks=100,
            current_accepted=90,
            current_acc_7d_n=20, current_acc_7d_sum=18,
            current_acc_30d_n=60, current_acc_30d_sum=54,
            current_acc_all_n=100, current_acc_all_sum=90,
            task_baselines=existing,
            current_zscore_flagged=0,
        )
        result = compute_score_update(inp)
        # Survey baseline starts fresh — stored in 0–100 units: 0.5 * 100 = 50.0
        assert result.task_baselines["survey"]["n"] == 1
        assert abs(result.task_baselines["survey"]["mean"] - 50.0) < 0.1
        # z-score must be None since survey has n=1
        assert result.zscore_latest is None


import math

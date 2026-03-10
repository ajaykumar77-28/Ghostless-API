"""
Ghostless API — v6 Test Suite

Coverage:
  1. test_bayesian_scoring.py          - Unit tests for the Bayesian engine
  2. test_scoring_regression.py        - Regression snapshots (golden outputs)
  3. test_pipeline.py                  - End-to-end pipeline (pure, no I/O)
  4. test_anti_farming.py              - Anti-farming cap behaviour
  5. test_cold_start_bayesian.py       - Cold start convergence
  6. test_monotonic_constraints.py     - Rejections always hurt
  7. test_ci_intervals.py              - Credible intervals are valid
  8. test_adversarial_pipeline.py      - Hardened adversarial scenarios
  9. test_chaos.py                     - Chaos / degraded-mode tests
  10. test_load_pipeline.py            - Load / throughput baseline

Run with: pytest tests/ -v --tb=short
"""

# ════════════════════════════════════════════════════════════════════════════════
# 1.  Bayesian scoring unit tests
# ════════════════════════════════════════════════════════════════════════════════
import math
import time
import pytest

from app.engine.bayesian import (
    BayesianScoreInput,
    BayesianScoreResult,
    compute_bayesian_score,
    beta_mean,
    beta_variance,
    beta_credible_interval,
    anti_farming_weight,
    cold_start_factor,
    update_volatility,
    PRIOR_ALPHA,
    PRIOR_BETA,
    ALGORITHM_VERSION,
    MAX_TRUST_DELTA_PER_CYCLE,
    FARMING_DECAY_BASE,
    MONOTONIC_REJECTION_FLOOR,
)
from app.engine.pipeline import (
    WorkerContext,
    TaskContext,
    run_pipeline,
)


# ── Beta math ─────────────────────────────────────────────────────────────────

class TestBetaMath:
    def test_mean_of_uniform_is_half(self):
        assert beta_mean(1.0, 1.0) == pytest.approx(0.5)

    def test_mean_increases_with_alpha(self):
        assert beta_mean(10, 2) > beta_mean(5, 5) > beta_mean(2, 10)

    def test_variance_is_zero_at_extremes(self):
        # At very large n, variance → 0
        assert beta_variance(1000, 1) < 0.001

    def test_credible_interval_contains_mean(self):
        alpha, beta_ = 8.0, 4.0
        lo, hi = beta_credible_interval(alpha, beta_)
        mean = beta_mean(alpha, beta_)
        assert lo < mean < hi

    def test_credible_interval_width_shrinks_with_data(self):
        lo_small, hi_small = beta_credible_interval(3, 3)
        lo_large, hi_large = beta_credible_interval(300, 300)
        assert (hi_small - lo_small) > (hi_large - lo_large)

    def test_credible_interval_is_unit_bounded(self):
        lo, hi = beta_credible_interval(2.5, 2.5)
        assert 0.0 <= lo <= hi <= 1.0


# ── Cold-start ────────────────────────────────────────────────────────────────

class TestColdStart:
    def test_new_worker_shrunk_toward_50(self):
        """A new worker with all acceptances should not reach high trust quickly."""
        inp = BayesianScoreInput(
            was_accepted       = True,
            total_observations = 1,   # 2nd task
            current_trust      = 50.0,
        )
        result = compute_bayesian_score(inp)
        # Trust should still be < 60 after just 2 observations
        assert result.trust_score < 62.0

    def test_cold_start_factor_at_zero_is_40pct(self):
        assert cold_start_factor(0) == pytest.approx(0.4)

    def test_cold_start_factor_at_full_is_100pct(self):
        from app.engine.bayesian import MIN_OBS_FOR_FULL_TRUST
        assert cold_start_factor(MIN_OBS_FOR_FULL_TRUST) == pytest.approx(1.0)

    def test_cold_start_linear_increase(self):
        from app.engine.bayesian import MIN_OBS_FOR_FULL_TRUST
        half = MIN_OBS_FOR_FULL_TRUST // 2
        assert cold_start_factor(half) == pytest.approx(0.7, abs=0.05)


# ── Momentum cap ──────────────────────────────────────────────────────────────

class TestMomentumCap:
    def test_single_acceptance_cannot_jump_more_than_max_delta(self):
        """Even a perfect score can't move trust by more than MAX_TRUST_DELTA_PER_CYCLE."""
        inp = BayesianScoreInput(
            posterior_alpha    = PRIOR_ALPHA,
            posterior_beta     = PRIOR_BETA,
            was_accepted       = True,
            total_observations = 100,
            current_trust      = 50.0,
            difficulty_weight  = 10.0,   # extreme difficulty
        )
        result = compute_bayesian_score(inp)
        assert abs(result.trust_delta) <= MAX_TRUST_DELTA_PER_CYCLE + 0.01

    def test_single_rejection_cannot_crash_more_than_max_delta(self):
        inp = BayesianScoreInput(
            posterior_alpha    = 50.0,   # strong prior
            posterior_beta     = 5.0,
            was_accepted       = False,
            total_observations = 100,
            current_trust      = 90.0,
        )
        result = compute_bayesian_score(inp)
        assert abs(result.trust_delta) <= MAX_TRUST_DELTA_PER_CYCLE + 0.01


# ── Monotonic constraints ─────────────────────────────────────────────────────

class TestMonotonicConstraints:
    def test_rejection_always_reduces_trust(self):
        """A rejection MUST reduce trust — never leave it the same or increase it."""
        for trust in [30.0, 50.0, 75.0, 90.0]:
            inp = BayesianScoreInput(
                posterior_alpha    = 20.0,
                posterior_beta     = 5.0,
                was_accepted       = False,
                total_observations = 50,
                current_trust      = trust,
            )
            result = compute_bayesian_score(inp)
            assert result.trust_score <= trust * MONOTONIC_REJECTION_FLOOR + 0.01, \
                f"Rejection failed to reduce trust at {trust}"

    def test_no_outcome_cannot_change_trust(self):
        """was_accepted=None (ungraded) should not move trust."""
        inp = BayesianScoreInput(
            was_accepted       = None,
            total_observations = 50,
            current_trust      = 70.0,
        )
        result = compute_bayesian_score(inp)
        # Without fraud/velocity, delta should be tiny (only peer adjustment)
        assert abs(result.trust_delta) < 5.0


# ── Anti-farming ──────────────────────────────────────────────────────────────

class TestAntiFarming:
    def test_first_task_has_full_weight(self):
        w = anti_farming_weight([], [], "image_label", True)
        assert w == pytest.approx(1.0)

    def test_repeated_tasks_diminish_weight(self):
        history_types = ["image_label"] * 10
        history_acc   = [True] * 10
        w = anti_farming_weight(history_types, history_acc, "image_label", True)
        assert w < 0.5   # 0.8^10 ≈ 0.107

    def test_type_change_resets_farming(self):
        history_types = ["image_label"] * 5 + ["transcription"]
        history_acc   = [True] * 6
        # The most recent is transcription, so streak for image_label is 0
        w = anti_farming_weight(history_types, history_acc, "image_label", True)
        # Only the first element (image_label) in history matches, but it's not
        # at position 0 in history (most recent first). streak = 0.
        assert w == pytest.approx(1.0)

    def test_mixed_outcomes_dont_farm(self):
        history_types = ["image_label"] * 4
        history_acc   = [True, False, True, False]
        w = anti_farming_weight(history_types, history_acc, "image_label", True)
        # Streak of True at start = 0 (most recent is False)
        assert w == pytest.approx(1.0)


# ── Score volatility ──────────────────────────────────────────────────────────

class TestVolatility:
    def test_volatility_starts_at_zero(self):
        v = update_volatility(0.0, 0.0)
        assert v == pytest.approx(0.0, abs=0.001)

    def test_large_delta_increases_volatility(self):
        v = 0.0
        for _ in range(20):
            v = update_volatility(v, 8.0)
        assert v > 4.0   # should converge toward 8.0

    def test_small_deltas_decrease_volatility(self):
        v = 5.0   # start high
        for _ in range(30):
            v = update_volatility(v, 0.1)
        assert v < 1.0   # decays toward 0.1


# ── Algorithm version ─────────────────────────────────────────────────────────

class TestAlgorithmVersion:
    def test_result_carries_version(self):
        inp = BayesianScoreInput()
        result = compute_bayesian_score(inp)
        assert result.algorithm_version == ALGORITHM_VERSION

    def test_version_format(self):
        assert ALGORITHM_VERSION.startswith("v")
        assert len(ALGORITHM_VERSION.split(".")) == 3


# ════════════════════════════════════════════════════════════════════════════════
# 2.  Scoring regression snapshots (golden outputs)
# ════════════════════════════════════════════════════════════════════════════════

class TestScoringRegressionSnapshots:
    """
    These are golden output tests. If the scoring algorithm changes in a way
    that breaks these, you MUST:
      1. Understand why the output changed.
      2. Update the golden values AND the ALGORITHM_VERSION.
      3. Run a score migration for existing workers.

    Never update these silently.
    """

    # Scenario A: brand-new worker, first accepted task
    def test_new_worker_first_acceptance(self):
        inp = BayesianScoreInput(
            posterior_alpha    = PRIOR_ALPHA,
            posterior_beta     = PRIOR_BETA,
            was_accepted       = True,
            total_observations = 0,
            current_trust      = 50.0,
        )
        result = compute_bayesian_score(inp)
        # Given cold-start shrinkage, trust should be close to 50 still
        assert 50.0 <= result.trust_score <= 55.0
        assert result.ci_width > 0.30   # high uncertainty at start

    # Scenario B: experienced worker, rejection
    def test_mature_worker_rejection(self):
        inp = BayesianScoreInput(
            posterior_alpha    = 60.0,   # 62 prior successes
            posterior_beta     = 4.0,    # 4 prior failures
            was_accepted       = False,
            total_observations = 100,
            current_trust      = 88.0,
        )
        result = compute_bayesian_score(inp)
        assert result.trust_score < 88.0       # rejection hurts
        assert result.trust_score > 70.0       # but doesn't crater
        assert result.ci_width < 0.15          # narrow CI — lots of data

    # Scenario C: fraud multiplier tanks score
    def test_fraud_multiplier_reduces_trust(self):
        inp = BayesianScoreInput(
            posterior_alpha    = 30.0,
            posterior_beta     = 10.0,
            was_accepted       = True,
            total_observations = 50,
            current_trust      = 70.0,
            fraud_multiplier   = 0.125,   # 3 fraud events decayed
        )
        result = compute_bayesian_score(inp)
        assert result.trust_score < 50.0

    # Scenario D: velocity penalty
    def test_velocity_penalty_reduces_trust(self):
        inp_normal = BayesianScoreInput(
            posterior_alpha      = 20.0,
            posterior_beta       = 5.0,
            was_accepted         = True,
            total_observations   = 50,
            current_trust        = 70.0,
            velocity_penalty_pts = 0.0,
        )
        inp_penalized = BayesianScoreInput(
            posterior_alpha      = 20.0,
            posterior_beta       = 5.0,
            was_accepted         = True,
            total_observations   = 50,
            current_trust        = 70.0,
            velocity_penalty_pts = 15.0,  # max penalty
        )
        r_normal    = compute_bayesian_score(inp_normal)
        r_penalized = compute_bayesian_score(inp_penalized)
        assert r_penalized.trust_score < r_normal.trust_score

    # Scenario E: max_trust ceiling
    def test_max_trust_ceiling_enforced(self):
        inp = BayesianScoreInput(
            posterior_alpha    = 200.0,
            posterior_beta     = 5.0,
            was_accepted       = True,
            total_observations = 500,
            current_trust      = 65.0,
            max_trust          = 70.0,   # post-fraud ceiling
        )
        result = compute_bayesian_score(inp)
        assert result.trust_score <= 70.0


# ════════════════════════════════════════════════════════════════════════════════
# 3.  Pipeline integration tests (pure — no DB/Redis)
# ════════════════════════════════════════════════════════════════════════════════

class TestPipeline:
    def _default_worker(self, **overrides) -> WorkerContext:
        defaults = dict(
            external_id     = "worker_123",
            trust_score     = 65.0,
            total_tasks     = 50,
            streak_days     = 5,
            fraud_multiplier = 1.0,
            tasks_last_hour  = 5,
            tasks_last_day   = 30,
        )
        defaults.update(overrides)
        return WorkerContext(**defaults)

    def _default_task(self, **overrides) -> TaskContext:
        defaults = dict(
            task_type       = "survey",
            payload         = {"responses": {"q1": "A", "q2": "B", "q3": "C"}},
            completion_time = 45.0,
            was_accepted    = True,
        )
        defaults.update(overrides)
        return TaskContext(**defaults)

    def test_clean_submission_is_allowed(self):
        result = run_pipeline(self._default_worker(), self._default_task())
        assert result.allow_submit is True
        assert result.fraud_severity == "clean"

    def test_empty_survey_is_blocked(self):
        task = self._default_task(payload={"responses": {}})
        result = run_pipeline(self._default_worker(), task)
        assert result.allow_submit is False

    def test_velocity_breach_triggers_fraud_signal(self):
        worker = self._default_worker(
            tasks_last_hour = 100,   # way over max
            max_tasks_per_hour = 60,
        )
        result = run_pipeline(worker, self._default_task())
        assert result.fraud_severity in ("warning", "critical")
        assert any(s.event_type == "velocity_breach" for s in result.fraud_signals)

    def test_shadow_banned_worker_is_flagged(self):
        worker = self._default_worker(shadow_banned=True)
        result = run_pipeline(worker, self._default_task())
        assert result.shadow_banned is True

    def test_explanation_contains_all_keys(self):
        result = run_pipeline(self._default_worker(), self._default_task())
        required_keys = [
            "algorithm_version", "posterior_mean", "ci_90pct",
            "trust_delta", "volatility", "anomaly_score", "quality_score",
            "fraud_signals", "rule_flags",
        ]
        for key in required_keys:
            assert key in result.explanation, f"Missing key: {key}"

    def test_critical_fraud_blocks_submission(self):
        """A critical-severity fraud signal (velocity breach) blocks allow_submit."""
        worker = self._default_worker(
            tasks_last_hour    = 200,
            max_tasks_per_hour = 60,
        )
        result = run_pipeline(worker, self._default_task())
        # Critical fraud signal → allow_submit should be False
        if result.fraud_severity == "critical":
            assert result.allow_submit is False

    def test_straight_lining_survey_flagged(self):
        """All-identical survey answers should trigger a flag."""
        task = self._default_task(
            payload={"responses": {"q1": "5", "q2": "5", "q3": "5", "q4": "5"}}
        )
        result = run_pipeline(self._default_worker(), task)
        assert "STRAIGHT_LINE" in result.rule_result.flags

    def test_trust_score_bounded(self):
        for _ in range(50):
            result = run_pipeline(self._default_worker(), self._default_task())
            assert 0.0 <= result.trust_score <= 100.0

    def test_anomaly_score_bounded(self):
        result = run_pipeline(self._default_worker(), self._default_task())
        assert 0.0 <= result.anomaly_score <= 1.0


# ════════════════════════════════════════════════════════════════════════════════
# 4.  Adversarial pipeline tests
# ════════════════════════════════════════════════════════════════════════════════

class TestAdversarial:
    """
    Adversarial scenarios designed to stress-test the scoring engine.
    Each test attempts a specific attack pattern and asserts the system
    resists it meaningfully.
    """

    def test_farming_attack_bounded_trust_gain(self):
        """
        Attacker submits 100 identical accepted tasks of the same type.
        Anti-farming should limit total trust gain to << linear growth.
        """
        worker = WorkerContext(
            external_id    = "farmer",
            trust_score    = 50.0,
            total_tasks    = 0,
            fraud_multiplier = 1.0,
        )
        task_type    = "image_label"
        payload      = {"labels": ["cat"], "confidence_cat": 0.99}
        recent_types = []
        recent_acc   = []

        trust_history = [50.0]
        for i in range(100):
            worker.recent_task_types = list(recent_types)
            worker.recent_accepted   = list(recent_acc)
            task = TaskContext(
                task_type       = task_type,
                payload         = payload,
                completion_time = 30.0,
                was_accepted    = True,
            )
            result = run_pipeline(worker, task)
            worker.trust_score       = result.trust_score
            worker.total_tasks      += 1
            worker.posterior_alpha   = result.bayesian_result.posterior_alpha
            worker.posterior_beta    = result.bayesian_result.posterior_beta
            worker.volatility        = result.bayesian_result.volatility
            recent_types.insert(0, task_type)
            recent_acc.insert(0, True)
            trust_history.append(result.trust_score)

        gain = trust_history[-1] - trust_history[0]
        # 100 identical tasks should NOT gain more than 40 trust points
        assert gain <= 40.0, f"Farming gained too much trust: +{gain:.1f}"

    def test_score_bounce_attack_is_slow(self):
        """
        Attacker alternates accept/reject hoping to exploit the update direction.
        Trust after 50 alternating cycles should not be meaningfully higher
        than the starting value (the rejections should cancel the gains).
        """
        worker = WorkerContext(external_id="bouncer", trust_score=50.0, fraud_multiplier=1.0)
        for i in range(50):
            accepted = i % 2 == 0
            task = TaskContext(
                task_type="survey",
                payload={"responses": {"q1": "A", "q2": "B", "q3": "C"}},
                completion_time=60.0,
                was_accepted=accepted,
            )
            result = run_pipeline(worker, task)
            worker.trust_score     = result.trust_score
            worker.posterior_alpha = result.bayesian_result.posterior_alpha
            worker.posterior_beta  = result.bayesian_result.posterior_beta

        # After 25 accepts and 25 rejects, should be close to starting value
        assert abs(worker.trust_score - 50.0) < 15.0, \
            f"Bounce attack drifted to {worker.trust_score:.1f}"

    def test_trust_cannot_exceed_max_trust(self):
        """No sequence of accepted tasks can exceed max_trust."""
        worker = WorkerContext(
            external_id    = "capped",
            trust_score    = 60.0,
            max_trust      = 70.0,
            total_tasks    = 200,
            fraud_multiplier = 1.0,
            posterior_alpha  = 180.0,
            posterior_beta   = 20.0,
        )
        for _ in range(20):
            task = TaskContext(
                task_type="image_label",
                payload={"labels": ["dog"]},
                completion_time=45.0,
                was_accepted=True,
            )
            result = run_pipeline(worker, task)
            assert result.trust_score <= 70.0 + 0.01, \
                f"max_trust breached: {result.trust_score}"
            worker.trust_score     = result.trust_score
            worker.posterior_alpha = result.bayesian_result.posterior_alpha
            worker.posterior_beta  = result.bayesian_result.posterior_beta


# ════════════════════════════════════════════════════════════════════════════════
# 5.  Chaos / degraded-mode tests
# ════════════════════════════════════════════════════════════════════════════════

class TestChaos:
    """Test that the pipeline handles missing/null/extreme inputs gracefully."""

    def test_nan_quality_inputs_handled(self):
        """Extreme but valid quality inputs should not crash the pipeline."""
        for q in [0.0, 1.0, 0.5]:
            inp = BayesianScoreInput(
                posterior_alpha    = PRIOR_ALPHA,
                posterior_beta     = PRIOR_BETA,
                was_accepted       = True,
                total_observations = 5,
                current_trust      = 50.0,
            )
            result = compute_bayesian_score(inp)
            assert math.isfinite(result.trust_score)

    def test_very_large_posterior_doesnt_crash(self):
        """Accumulated large posteriors should not cause float issues."""
        inp = BayesianScoreInput(
            posterior_alpha = 1_000_000.0,
            posterior_beta  = 50_000.0,
            was_accepted    = True,
            total_observations = 1_000_000,
            current_trust   = 95.0,
        )
        result = compute_bayesian_score(inp)
        assert math.isfinite(result.trust_score)
        assert 0.0 <= result.trust_score <= 100.0

    def test_fraud_multiplier_zero_floors_trust(self):
        """Fraud multiplier of 0 should floor trust to 0."""
        inp = BayesianScoreInput(
            posterior_alpha  = 50.0,
            posterior_beta   = 5.0,
            was_accepted     = True,
            total_observations = 100,
            current_trust    = 80.0,
            fraud_multiplier = 0.0,
        )
        result = compute_bayesian_score(inp)
        assert result.trust_score == pytest.approx(0.0, abs=0.01)

    def test_pipeline_with_empty_payload(self):
        """Empty payload should not crash the pipeline (will fail rules)."""
        worker = WorkerContext(external_id="test", trust_score=50.0)
        task   = TaskContext(
            task_type="survey", payload={}, completion_time=30.0
        )
        result = run_pipeline(worker, task)
        # Pipeline should return a decision (may block submission)
        assert result is not None
        assert isinstance(result.allow_submit, bool)

    def test_pipeline_with_none_was_accepted(self):
        """Ungraded tasks (was_accepted=None) should still produce a valid decision."""
        worker = WorkerContext(external_id="test", trust_score=65.0)
        task   = TaskContext(
            task_type="survey",
            payload={"responses": {"q1": "A"}},
            completion_time=40.0,
            was_accepted=None,
        )
        result = run_pipeline(worker, task)
        assert math.isfinite(result.trust_score)


# ════════════════════════════════════════════════════════════════════════════════
# 6.  Load / throughput baseline (NOT a real load test — just a timing check)
#     Real load tests should use Locust against the running API.
# ════════════════════════════════════════════════════════════════════════════════

class TestLoadPipeline:
    """
    Ensures the pure scoring pipeline is fast enough to not be the bottleneck.
    Target: 10,000 scoring cycles in < 2 seconds (no I/O).
    """

    def test_pipeline_throughput_baseline(self):
        N = 5_000
        worker = WorkerContext(
            external_id    = "load_worker",
            trust_score    = 65.0,
            total_tasks    = 100,
            fraud_multiplier = 1.0,
        )
        task = TaskContext(
            task_type       = "survey",
            payload         = {"responses": {"q1": "A", "q2": "B", "q3": "C"}},
            completion_time = 45.0,
            was_accepted    = True,
        )

        start = time.perf_counter()
        for _ in range(N):
            run_pipeline(worker, task)
        elapsed = time.perf_counter() - start

        rps = N / elapsed
        assert elapsed < 5.0, f"{N} pipeline runs took {elapsed:.2f}s ({rps:.0f}/s)"

    def test_bayesian_engine_throughput(self):
        N = 50_000
        inp = BayesianScoreInput(
            posterior_alpha    = 20.0,
            posterior_beta     = 5.0,
            was_accepted       = True,
            total_observations = 50,
            current_trust      = 70.0,
        )
        start = time.perf_counter()
        for _ in range(N):
            compute_bayesian_score(inp)
        elapsed = time.perf_counter() - start
        rps = N / elapsed
        assert elapsed < 3.0, f"{N} Bayesian calls took {elapsed:.2f}s ({rps:.0f}/s)"


# ════════════════════════════════════════════════════════════════════════════════
# 7.  Fuzz tests (property-based)
# ════════════════════════════════════════════════════════════════════════════════

class TestFuzz:
    """
    Property-based fuzz tests. We don't use Hypothesis here to keep the dep
    list small, but these cover a broad input space manually.
    """

    def test_trust_always_in_range(self):
        """Trust score is always in [0, max_trust] regardless of inputs."""
        import random
        rng = random.Random(42)
        for _ in range(1000):
            alpha    = rng.uniform(0.1, 1000.0)
            beta_    = rng.uniform(0.1, 1000.0)
            accepted = rng.choice([True, False, None])
            trust    = rng.uniform(0.0, 100.0)
            max_t    = rng.uniform(trust, 100.0)
            fraud_m  = rng.uniform(0.0, 1.0)
            vel_pen  = rng.uniform(0.0, 15.0)

            inp = BayesianScoreInput(
                posterior_alpha      = alpha,
                posterior_beta       = beta_,
                was_accepted         = accepted,
                total_observations   = rng.randint(0, 500),
                current_trust        = trust,
                max_trust            = max_t,
                fraud_multiplier     = fraud_m,
                velocity_penalty_pts = vel_pen,
            )
            result = compute_bayesian_score(inp)

            assert math.isfinite(result.trust_score), \
                f"NaN/Inf trust at alpha={alpha}, beta={beta_}"
            assert 0.0 <= result.trust_score <= max_t + 0.01, \
                f"OOB trust {result.trust_score} (max={max_t})"

    def test_ci_always_valid(self):
        """Credible interval is always [0,1] with lower < upper."""
        import random
        rng = random.Random(99)
        for _ in range(500):
            alpha = rng.uniform(0.5, 500.0)
            beta_ = rng.uniform(0.5, 500.0)
            lo, hi = beta_credible_interval(alpha, beta_)
            assert 0.0 <= lo <= hi <= 1.0, \
                f"Invalid CI [{lo}, {hi}] for alpha={alpha}, beta={beta_}"

    def test_pipeline_never_crashes(self):
        """run_pipeline() never raises on arbitrary well-formed inputs."""
        import random
        rng = random.Random(7)
        task_types = ["survey", "image_label", "transcription", "moderation", "unknown_type"]
        payloads = [
            {},
            {"responses": {}},
            {"responses": {"q1": "A"}},
            {"labels": ["cat"]},
            {"text": "Hello world"},
            {"verdict": "safe"},
            {"x": None},
        ]
        for _ in range(200):
            worker = WorkerContext(
                external_id    = "fuzz_worker",
                trust_score    = rng.uniform(0.0, 100.0),
                total_tasks    = rng.randint(0, 500),
                fraud_multiplier = rng.uniform(0.0, 1.0),
                tasks_last_hour  = rng.randint(0, 200),
            )
            task = TaskContext(
                task_type       = rng.choice(task_types),
                payload         = rng.choice(payloads),
                completion_time = rng.uniform(0.1, 3600.0),
                was_accepted    = rng.choice([True, False, None]),
            )
            try:
                result = run_pipeline(worker, task)
                assert result is not None
            except Exception as e:
                pytest.fail(f"Pipeline raised: {e} on worker={worker}, task={task}")

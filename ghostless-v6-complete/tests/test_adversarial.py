"""
Adversarial Tests — workers actively trying to game the scoring system.

Each test models a specific attack strategy and asserts that the scoring
and fraud systems degrade or catch the worker rather than rewarding them.

Attack catalogue:
  A1  Score sandbagging       — build low baseline, then dump perfects
  A2  Threshold camping        — hover just below anomaly z-score cut-off
  A3  EWMA pinning             — alternate high/low to keep EWMA artificially stable
  A4  Confidence window abuse  — behave well until full confidence, then flip
  A5  Fraud cap exploitation   — keep fraud events exactly at penalty cap
  A6  Speed threshold surfing  — submit at exactly the warning boundary
  A7  Hash mutation evasion    — tweak payload minimally to escape exact dedup
  A8  Sybil new-account reset  — start new account to shed bad confidence weight
  A9  Streak burn-and-restore  — burn streak deliberately to reset detection baseline
  A10 Penalty dilution         — interleave fraudulent with clean tasks to dilute signal
"""
import math
import pytest

from app.services.scoring import (
    CONFIDENCE_FLOOR,
    CONFIDENCE_FULL_AT_N,
    EWMA_ALPHA_MATURE,
    FRAUD_PENALTY_MAX,
    FRAUD_PENALTY_PER_EVENT,
    ZSCORE_ANOMALY_THRESHOLD,
    ZSCORE_FRAUD_THRESHOLD,
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
from app.services.fraud import (
    check_answer_similarity,
    check_ip_sharing,
    check_velocity,
    run_fraud_checks,
    structural_hash,
    tokenize_payload,
)
from app.services.rule_engine import RuleEngine

engine = RuleEngine()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _blank_input(**overrides) -> ScoreUpdateInput:
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
        current_acc_7d_n=5,  current_acc_7d_sum=4,
        current_acc_30d_n=5, current_acc_30d_sum=4,
        current_acc_all_n=5, current_acc_all_sum=4,
        task_baselines={},
        current_zscore_flagged=0,
    )
    defaults.update(overrides)
    return ScoreUpdateInput(**defaults)


def _simulate_n_tasks(n: int, quality: float, starting_input: ScoreUpdateInput) -> ScoreUpdateResult:
    """Drive n sequential tasks through the scoring engine and return final result."""
    inp = starting_input
    result = None
    for i in range(n):
        result = compute_score_update(inp)
        inp = ScoreUpdateInput(
            quality_score=quality,
            task_type=inp.task_type,
            was_accepted=True,
            fraud_event_count=inp.fraud_event_count,
            streak_days=inp.streak_days,
            days_since_joined=inp.days_since_joined + (i / 365),
            current_ewma=result.ewma_quality,
            current_total_tasks=inp.current_total_tasks + 1,
            current_accepted=inp.current_accepted + 1,
            current_acc_7d_n=inp.current_acc_7d_n + 1,
            current_acc_7d_sum=inp.current_acc_7d_sum + 1,
            current_acc_30d_n=inp.current_acc_30d_n + 1,
            current_acc_30d_sum=inp.current_acc_30d_sum + 1,
            current_acc_all_n=inp.current_acc_all_n + 1,
            current_acc_all_sum=inp.current_acc_all_sum + 1,
            task_baselines=result.task_baselines,
            current_zscore_flagged=result.zscore_flagged_count,
        )
    return result


# ─── A1: Score sandbagging ────────────────────────────────────────────────────

class TestScoreSandbagging:
    """
    Attack: Worker intentionally submits mediocre tasks early to train a low
    peer baseline, then switches to perfect scores. Hopes the "perfect" scores
    look normal against the low baseline they helped create.

    Defence: Confidence weighting — EWMA history punishes the phase-1 period.
    The transition also creates a z-score spike that should be detectable.
    """

    def test_sandbagged_ewma_is_lower_than_genuine_perfect(self):
        """A worker who was mediocre then perfect has lower EWMA than always-perfect."""
        # Phase 1: 30 mediocre tasks at 0.4 quality
        bad_start = _blank_input(
            quality_score=0.4, current_ewma=0.5,
            current_total_tasks=0, current_accepted=0,
            current_acc_all_n=0, current_acc_all_sum=0,
        )
        mediocre_result = _simulate_n_tasks(30, 0.4, bad_start)

        # Phase 2: 20 "perfect" tasks
        perfect_after_sandbagging = ScoreUpdateInput(
            quality_score=1.0, task_type="image_label",
            was_accepted=True, fraud_event_count=0,
            streak_days=5, days_since_joined=60.0,
            current_ewma=mediocre_result.ewma_quality,
            current_total_tasks=30, current_accepted=25,
            current_acc_7d_n=20, current_acc_7d_sum=17,
            current_acc_30d_n=30, current_acc_30d_sum=25,
            current_acc_all_n=30, current_acc_all_sum=25,
            task_baselines=mediocre_result.task_baselines,
            current_zscore_flagged=mediocre_result.zscore_flagged_count,
        )
        sandbagged_result = _simulate_n_tasks(20, 1.0, perfect_after_sandbagging)

        # Genuine perfect worker from day 0
        genuine_start = _blank_input(
            quality_score=1.0, current_ewma=0.5,
            current_total_tasks=0, current_accepted=0,
            current_acc_all_n=0, current_acc_all_sum=0,
        )
        genuine_result = _simulate_n_tasks(50, 1.0, genuine_start)

        # Sandbagged EWMA must be lower — history matters
        assert sandbagged_result.ewma_quality < genuine_result.ewma_quality, (
            f"Sandbagger EWMA {sandbagged_result.ewma_quality:.4f} should be "
            f"below genuine {genuine_result.ewma_quality:.4f}"
        )

    def test_sandbagged_trust_score_penalised(self):
        """End trust score of sandbagging worker < always-honest worker."""
        bad_start = _blank_input(current_ewma=0.3, current_total_tasks=30)
        mediocre_result = _simulate_n_tasks(20, 0.35, bad_start)

        sandbagged = ScoreUpdateInput(
            quality_score=1.0, task_type="image_label",
            was_accepted=True, fraud_event_count=0,
            streak_days=0, days_since_joined=60.0,
            current_ewma=mediocre_result.ewma_quality,
            current_total_tasks=50, current_accepted=40,
            current_acc_7d_n=10, current_acc_7d_sum=9,
            current_acc_30d_n=20, current_acc_30d_sum=16,
            current_acc_all_n=50, current_acc_all_sum=40,
            task_baselines=mediocre_result.task_baselines,
            current_zscore_flagged=0,
        )
        sandbagged_final = compute_score_update(sandbagged)

        honest_start = _blank_input(
            current_ewma=0.9, current_total_tasks=50,
            current_accepted=48, current_acc_all_n=50, current_acc_all_sum=48,
        )
        honest_result = compute_score_update(honest_start)

        assert sandbagged_final.trust_score < honest_result.trust_score

    def test_phase_transition_creates_zscore_spike(self):
        """
        Switching from ~0.3 quality to 1.0 quality against an established
        low-quality peer baseline should produce a large positive z-score.
        We build a peer baseline with LOW mean and non-zero variance (varied
        scores around 30), then submit 100 (perfect). This must be anomalous.
        """
        # Build a low peer baseline with natural variance (not all identical)
        import random
        random.seed(42)
        bl = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(ZSCORE_MIN_PEER_SAMPLE + 10):
            # Values cluster around 30 with small natural variation: 25–35
            bl = welford_update(bl, 30.0 + (i % 11) - 5)

        # Verify baseline has non-zero std before testing z-score
        assert welford_std(bl) > 1e-6, (
            f"Test baseline must have nonzero std, got {welford_std(bl):.6f}"
        )

        # Now this worker submits "perfect" = 100.0 score units
        z = compute_zscore(100.0, bl)
        assert z is not None, (
            "Z-score must be computable: baseline has enough samples and nonzero std"
        )
        assert z > ZSCORE_ANOMALY_THRESHOLD, (
            f"Expected z > {ZSCORE_ANOMALY_THRESHOLD}, got {z:.2f}. "
            "Phase transition from sandbagged baseline (mean≈30) to perfect (100) "
            "should look strongly anomalous."
        )


# ─── A2: Threshold camping ────────────────────────────────────────────────────

class TestThresholdCamping:
    """
    Attack: Worker learns the anomaly z-score threshold (2.5) and submits
    scores that produce z ≈ 2.4 — just below detection — repeatedly.

    Defence: zscore_flagged_count accumulates over time. Even near-threshold
    scores should accumulate a meaningful count of near-miss flags over 50+ tasks.
    """

    def test_z_at_2499_does_not_trigger_anomaly(self):
        """Confirm boundary: z = 2.499 should NOT flag as anomaly."""
        n   = 50
        std = 10.0
        m2  = std ** 2 * n
        bl  = {"mean": 50.0, "m2": m2, "n": n}
        # Score that produces z = 2.499
        target_score = 50.0 + 2.499 * std
        z = compute_zscore(target_score, bl)
        assert z is not None
        assert abs(z) < ZSCORE_ANOMALY_THRESHOLD
        # This means the camping worker evades single-task detection — which is correct.
        # But we assert it doesn't flip to fraud either.
        assert abs(z) < ZSCORE_FRAUD_THRESHOLD

    def test_z_at_2501_does_trigger_anomaly(self):
        """Confirm boundary: z = 2.501 SHOULD flag."""
        n   = 50
        std = 10.0
        m2  = std ** 2 * n
        bl  = {"mean": 50.0, "m2": m2, "n": n}
        target_score = 50.0 + 2.501 * std
        z = compute_zscore(target_score, bl)
        assert z is not None and abs(z) > ZSCORE_ANOMALY_THRESHOLD

    def test_camping_worker_gradually_poisons_their_own_baseline(self):
        """
        Worker who consistently camps just below threshold shifts the
        EWMA toward their inflated score — which eventually raises their baseline
        and makes future inflation harder, not easier.
        """
        # Worker starts at 0.5 EWMA and tries to camp at 0.75 quality
        ewma = 0.5
        for _ in range(100):
            ewma = update_ewma(ewma, 0.75, EWMA_ALPHA_MATURE)

        # EWMA should now reflect the 0.75 quality, making further gains harder
        assert ewma > 0.70, "EWMA should have shifted toward the camped value"
        # The camping "works" in moving EWMA up, but a sudden jump to 0.99
        # will now look anomalous against this 0.75 baseline.
        ewma_after_jump = update_ewma(ewma, 0.99, EWMA_ALPHA_MATURE)
        delta = ewma_after_jump - ewma
        assert delta < 0.03, "Single task should not be able to jump EWMA > 3pts at mature alpha"


# ─── A3: EWMA pinning ─────────────────────────────────────────────────────────

class TestEWMAPinning:
    """
    Attack: Worker alternates between high (0.95) and low (0.35) submissions
    hoping to pin EWMA near 0.65 while actually producing inconsistent work.

    Defence: EWMA reflects both extremes. Inconsistency causes zscore_flagged
    to accumulate on the extreme-low submissions.
    """

    def test_alternating_quality_ewma_reflects_average(self):
        """EWMA of alternating 0.35/0.95 should converge near their mean."""
        ewma = 0.65
        for i in range(200):
            val = 0.95 if i % 2 == 0 else 0.35
            ewma = update_ewma(ewma, val, EWMA_ALPHA_MATURE)

        expected_mean = (0.95 + 0.35) / 2
        assert abs(ewma - expected_mean) < 0.05, (
            f"EWMA {ewma:.4f} should be near alternating mean {expected_mean}"
        )

    def test_pinned_ewma_is_same_as_honest_average(self):
        """A pinner and an honest-average worker score comparably — pinner gets no edge."""
        # Honest worker at consistent 0.65
        honest_ewma = 0.65
        for _ in range(100):
            honest_ewma = update_ewma(honest_ewma, 0.65, EWMA_ALPHA_MATURE)

        # Pinner alternating 0.35/0.95
        pinner_ewma = 0.65
        for i in range(100):
            val = 0.95 if i % 2 == 0 else 0.35
            pinner_ewma = update_ewma(pinner_ewma, val, EWMA_ALPHA_MATURE)

        assert abs(pinner_ewma - honest_ewma) < 0.05, (
            "Pinner EWMA should not deviate >5pts from honest worker at same average"
        )

    def test_low_submissions_in_pinning_pattern_accumulate_flags(self):
        """The 0.35-quality submissions in the pattern are below the peer mean."""
        # Build peer baseline around 0.65 with natural variance
        bl = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(40):
            # Values cluster around 65 with ±5 variation
            bl = welford_update(bl, 65.0 + (i % 11) - 5)

        std = welford_std(bl)
        assert std > 1e-6, f"Peer baseline must have nonzero std, got {std}"

        # The low submission: 0.35 * 100 = 35.0
        z_low = compute_zscore(35.0, bl)
        assert z_low is not None
        # Should be significantly below mean (negative z)
        assert z_low < 0
        # Each low submission should be anomalous given consistent 0.65 peer baseline
        assert abs(z_low) > ZSCORE_ANOMALY_THRESHOLD, (
            f"Low submissions in pin pattern (z={z_low:.2f}) should trigger anomaly flag"
        )


# ─── A4: Confidence window abuse ──────────────────────────────────────────────

class TestConfidenceWindowAbuse:
    """
    Attack: Worker behaves perfectly until reaching full confidence weight
    (50 accepted tasks), then degrades to minimal quality.

    Defence: EWMA history — bad tasks after reaching full confidence will
    drag down EWMA. The mature alpha (0.10) means bad behaviour is slow to
    recover but steady history is also slow to erase. We verify the EWMA
    penalises the flip.
    """

    def test_flip_after_full_confidence_damages_ewma(self):
        """EWMA after 50 good + 20 bad is lower than 50 good + 20 good."""
        # Phase 1: 50 tasks at 0.9 quality (reach full confidence)
        inp = _blank_input(
            quality_score=0.9, current_ewma=0.5,
            current_total_tasks=0, current_accepted=0,
            current_acc_all_n=0, current_acc_all_sum=0,
        )
        result = _simulate_n_tasks(50, 0.9, inp)
        assert result.confidence_weight_val == pytest.approx(1.0, abs=0.05)

        # Phase 2a: 20 bad tasks (the "flip")
        flip_inp = ScoreUpdateInput(
            quality_score=0.2, task_type="image_label",
            was_accepted=False, fraud_event_count=0,
            streak_days=0, days_since_joined=100.0,
            current_ewma=result.ewma_quality,
            current_total_tasks=50, current_accepted=48,
            current_acc_7d_n=20, current_acc_7d_sum=18,
            current_acc_30d_n=30, current_acc_30d_sum=28,
            current_acc_all_n=50, current_acc_all_sum=48,
            task_baselines=result.task_baselines,
            current_zscore_flagged=0,
        )
        flipped_result = _simulate_n_tasks(20, 0.2, flip_inp)

        # Phase 2b: 20 continued good tasks (baseline comparison)
        continue_inp = ScoreUpdateInput(
            quality_score=0.9, task_type="image_label",
            was_accepted=True, fraud_event_count=0,
            streak_days=5, days_since_joined=100.0,
            current_ewma=result.ewma_quality,
            current_total_tasks=50, current_accepted=48,
            current_acc_7d_n=20, current_acc_7d_sum=18,
            current_acc_30d_n=30, current_acc_30d_sum=28,
            current_acc_all_n=50, current_acc_all_sum=48,
            task_baselines=result.task_baselines,
            current_zscore_flagged=0,
        )
        continued_result = _simulate_n_tasks(20, 0.9, continue_inp)

        assert flipped_result.trust_score < continued_result.trust_score, (
            "Worker who flips to bad quality after full confidence should score lower "
            "than worker who continued honestly"
        )

    def test_full_confidence_weight_not_a_permanent_free_pass(self):
        """A worker at full confidence who goes to 0.0 quality loses score fast."""
        # Worker already at full confidence
        inp = _blank_input(
            current_ewma=0.85, current_accepted=50,
            current_acc_all_n=50, current_acc_all_sum=48,
            quality_score=0.0,
        )
        result_after_crash = _simulate_n_tasks(30, 0.0, inp)
        # Trust score should drop significantly
        baseline_result = compute_score_update(
            _blank_input(current_ewma=0.85, current_accepted=50, current_acc_all_n=50, current_acc_all_sum=48)
        )
        assert result_after_crash.trust_score < baseline_result.trust_score - 10, (
            "30 zero-quality tasks should reduce trust score by > 10 pts even at full confidence"
        )


# ─── A5: Fraud cap exploitation ───────────────────────────────────────────────

class TestFraudCapExploitation:
    """
    Attack: Worker knows the fraud penalty is capped at 20 pts, so they
    accumulate exactly enough fraud events to hit the cap and then behave
    "cleanly" to offset the fixed 20pt penalty with high EWMA.

    Defence: The cap is not a loophole — 20pt penalty is a permanent ceiling
    drag on score. We verify high EWMA can only partially compensate.
    """

    def test_max_fraud_penalty_permanently_caps_achievable_score(self):
        """Even perfect EWMA cannot overcome max fraud penalty to reach score 100."""
        import math
        # ceil ensures we're at or past the cap (e.g. 20/3 = 6.67 → ceil = 7)
        cap_events = math.ceil(FRAUD_PENALTY_MAX / FRAUD_PENALTY_PER_EVENT)
        inp = _blank_input(
            quality_score=1.0,
            current_ewma=1.0,
            fraud_event_count=cap_events,
            streak_days=30,
            days_since_joined=365.0,
            current_accepted=50,
            current_acc_all_n=50,
            current_acc_all_sum=50,
        )
        result = compute_score_update(inp)
        # Score should be meaningfully below 100 due to the cap
        assert result.trust_score <= 100.0 - FRAUD_PENALTY_MAX + 5, (
            f"Max fraud penalty should drag max score below {100 - FRAUD_PENALTY_MAX + 5}, "
            f"got {result.trust_score}"
        )

    def test_one_extra_fraud_event_beyond_cap_has_no_additional_effect(self):
        """The cap is a hard ceiling — extra events beyond it add 0 penalty."""
        import math
        cap_events     = math.ceil(FRAUD_PENALTY_MAX / FRAUD_PENALTY_PER_EVENT)
        beyond_events  = cap_events + 10

        at_cap   = compute_score_update(_blank_input(fraud_event_count=cap_events))
        beyond   = compute_score_update(_blank_input(fraud_event_count=beyond_events))

        assert at_cap.trust_score == pytest.approx(beyond.trust_score, abs=0.01), (
            f"Events beyond the fraud penalty cap should have zero additional effect. "
            f"at_cap={at_cap.trust_score:.2f}, beyond={beyond.trust_score:.2f}"
        )

    def test_fraud_penalty_strictly_greater_than_zero_at_one_event(self):
        clean = compute_score_update(_blank_input(fraud_event_count=0))
        dirty = compute_score_update(_blank_input(fraud_event_count=1))
        assert dirty.trust_score < clean.trust_score


# ─── A6: Speed threshold surfing ─────────────────────────────────────────────

class TestSpeedThresholdSurfing:
    """
    Attack: Worker learns the speed warning threshold (35% of avg completion time)
    and submits at exactly 36% to avoid any penalty.

    Defence: Just-above-threshold submissions should be completely clean.
    We verify the system is not overly trigger-happy near the boundary.
    """

    def _run_at_time_fraction(self, fraction: float) -> "RuleResult":
        avg = 120.0
        return engine.run(
            "default", {}, completion_time=avg * fraction,
            worker_history={"avg_completion_time": avg, "trust_score": 50}
        )

    def test_at_36pct_no_speed_flag(self):
        result = self._run_at_time_fraction(0.36)
        codes = [w.code for w in result.warnings]
        assert "SPEED_FLAG" not in codes
        assert "SPEED_WARNING" not in codes

    def test_at_34pct_triggers_warning_not_flag(self):
        result = self._run_at_time_fraction(0.34)
        codes = [w.code for w in result.warnings]
        assert "SPEED_WARNING" in codes
        assert "SPEED_FLAG" not in codes

    def test_at_14pct_triggers_error_flag(self):
        result = self._run_at_time_fraction(0.14)
        codes = [w.code for w in result.warnings]
        assert "SPEED_FLAG" in codes

    def test_surfer_who_stays_at_36pct_scores_same_as_normal(self):
        """Worker surfing just above threshold gets identical score to normal worker."""
        surfer = self._run_at_time_fraction(0.36)
        normal = self._run_at_time_fraction(1.0)
        assert surfer.quality_score == pytest.approx(normal.quality_score, abs=0.01)


# ─── A7: Hash mutation evasion ────────────────────────────────────────────────

class TestHashMutationEvasion:
    """
    Attack: Worker knows exact-hash dedup is in use and adds a noise field
    to each submission to produce a unique hash while submitting identical answers.

    Defence: Tokenisation still catches the near-identical content. Exact hash
    won't match, but the meaningful token set will be near-identical.
    We verify token sets are stable under noise-field mutation.
    """

    def test_adding_noise_field_changes_hash(self):
        base = {"q1": "yes", "q2": "no", "q3": "agree"}
        noisy = {**base, "_noise": "abc123"}
        assert structural_hash(base) != structural_hash(noisy)

    def test_noise_field_barely_changes_token_set(self):
        base  = {"q1": "yes", "q2": "no", "q3": "agree", "q4": "strongly_agree"}
        noisy = {**base, "_noise": "xyz999"}
        base_tokens  = tokenize_payload(base)
        noisy_tokens = tokenize_payload(noisy)
        # The meaningful tokens are almost all identical
        from app.services.fraud import jaccard_similarity
        sim = jaccard_similarity(base_tokens, noisy_tokens)
        # Similarity should be high — one extra noise token out of 5+
        assert sim > 0.70, f"Jaccard similarity {sim:.2f} — mutated payload should still be similar"

    def test_many_noise_fields_still_share_core_tokens(self):
        base = {"verdict": "safe", "confidence": "high", "category": "adult"}
        mutants = [
            {**base, f"_n{i}": f"noise_{i * 17}"}
            for i in range(20)
        ]
        base_tokens = tokenize_payload(base)
        for mutant in mutants:
            mutant_tokens = tokenize_payload(mutant)
            shared = base_tokens & mutant_tokens
            # All core tokens should be present in every mutant
            assert len(shared) >= len(base_tokens), (
                f"Core token set should survive noise mutation. Shared: {shared}"
            )

    def test_slightly_different_answer_breaks_exact_hash(self):
        """Changing one answer defeats exact hash but doesn't prevent clustering."""
        base   = {"q1": "yes", "q2": "no"}
        tweaked = {"q1": "yes", "q2": "NO"}  # different case
        assert structural_hash(base) != structural_hash(tweaked)
        # But tokens are case-normalised so they remain identical
        assert tokenize_payload(base) == tokenize_payload(tweaked)


# ─── A8: Sybil new-account reset ─────────────────────────────────────────────

class TestSybilAccountReset:
    """
    Attack: Worker with degraded trust creates a new account to reset
    confidence weight to CONFIDENCE_FLOOR and start fresh.

    Defence: New account legitimately gets low confidence weight — that IS the
    defence. A sybil worker cannot use the new account for leaderboard gaming
    because it starts at floor confidence. We verify the confidence ceiling
    is not reachable without real task history.
    """

    def test_brand_new_account_has_floor_confidence(self):
        cw = confidence_weight(0)
        assert cw == pytest.approx(CONFIDENCE_FLOOR)

    def test_new_account_cannot_reach_ceiling_in_few_tasks(self):
        for n in range(CONFIDENCE_FULL_AT_N):
            cw = confidence_weight(n)
            assert cw < 1.0, f"Should not reach full confidence at {n} tasks"

    def test_sybil_new_account_cannot_instantly_dominate_leaderboard(self):
        """New account with high quality still scores < veteran with same quality."""
        new_account = compute_score_update(_blank_input(
            current_ewma=0.95, current_total_tasks=0, current_accepted=0,
            current_acc_all_n=0, current_acc_all_sum=0,
            quality_score=1.0,
        ))
        veteran = compute_score_update(_blank_input(
            current_ewma=0.95, current_total_tasks=200, current_accepted=50,
            current_acc_all_n=50, current_acc_all_sum=48,
            quality_score=1.0,
        ))
        assert new_account.trust_score < veteran.trust_score, (
            "New sybil account should not be able to outrank a legitimate veteran"
        )


# ─── A9: Streak manipulation ──────────────────────────────────────────────────

class TestStreakManipulation:
    """
    Attack: Worker intentionally breaks their streak (logs off for a day) to
    reset some internal tracking state, hoping it creates a fresh baseline window.

    Defence: EWMA is independent of streak. Streak only contributes 10/100 pts max.
    Voluntarily breaking streak is self-harming, not beneficial.
    """

    def test_breaking_streak_strictly_reduces_score(self):
        with_streak    = compute_score_update(_blank_input(streak_days=20))
        without_streak = compute_score_update(_blank_input(streak_days=0))
        assert with_streak.trust_score > without_streak.trust_score

    def test_max_streak_bonus_bounded(self):
        """Streak bonus cannot exceed 10 points regardless of streak length."""
        long_streak  = compute_score_update(_blank_input(streak_days=999))
        short_streak = compute_score_update(_blank_input(streak_days=30))
        # Both should be capped at same streak contribution
        assert long_streak.trust_score == pytest.approx(short_streak.trust_score, abs=0.1)


# ─── A10: Penalty dilution ────────────────────────────────────────────────────

class TestPenaltyDilution:
    """
    Attack: Worker submits 1 fraudulent task every 10 clean tasks, hoping the
    high volume of clean tasks dilutes any fraud signal below detection.

    Defence: Fraud penalty is per unreviewed event, not per-rate. Each fraud
    event contributes full FRAUD_PENALTY_PER_EVENT regardless of dilution.
    """

    def test_one_fraud_per_ten_clean_accumulates_penalty(self):
        """50 tasks: 45 clean, 5 fraud. Should still suffer fraud penalty."""
        clean_result = compute_score_update(_blank_input(fraud_event_count=0))
        diluted_result = compute_score_update(_blank_input(fraud_event_count=5))
        expected_penalty = 5 * FRAUD_PENALTY_PER_EVENT
        delta = clean_result.trust_score - diluted_result.trust_score
        assert abs(delta - expected_penalty) < 1.0, (
            f"Expected penalty delta ~{expected_penalty}, got {delta:.2f}. "
            "Dilution ratio should not affect per-event penalty magnitude."
        )

    def test_fraud_penalty_additive_not_averaged(self):
        """3 events = 3× the penalty of 1 event, not some fraction."""
        one   = compute_score_update(_blank_input(fraud_event_count=1))
        three = compute_score_update(_blank_input(fraud_event_count=3))
        six   = compute_score_update(_blank_input(fraud_event_count=6))
        delta_1_3 = one.trust_score - three.trust_score
        delta_3_6 = three.trust_score - six.trust_score
        # Each step adds the same fixed penalty per event
        assert abs(delta_1_3 - delta_3_6) < 0.5, (
            "Fraud penalty should be strictly additive, not averaged or diluted"
        )

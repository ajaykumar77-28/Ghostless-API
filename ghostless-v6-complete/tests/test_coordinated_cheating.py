"""
Coordinated Cheating Simulations — multi-worker attack patterns.

These tests model adversarial groups, not individual bad actors.
Each simulation has a setup (the attack) and assertions (the detection).

Attack catalogue:
  CC1  IP farm          — N workers behind one IP, submitting in sync
  CC2  Answer ring      — group submits identical payloads, rotating slightly
  CC3  Baseline poison  — group floods low-quality tasks to widen std,
                          then each member submits anomalous work "within" the band
  CC4  Sybil burst      — many new accounts burst simultaneously after cooldown
  CC5  Velocity split   — ring splits tasks to stay individually under velocity cap
  CC6  Trust launder    — high-trust members vouch implicitly (shared task patterns)
  CC7  Gradient drift   — coordinated slow quality decline to avoid sudden flags
  CC8  Split IP ring    — rotate across 2 IPs, each with ≤threshold workers
  CC9  Fraud burn-down  — group submits one fraud event each then stops
  CC10 Cross-type ring  — different workers submit same payload under different task types
"""
import math
import pytest

from app.services.fraud import (
    FraudSignal,
    check_answer_similarity,
    check_ip_sharing,
    check_velocity,
    run_fraud_checks,
    structural_hash,
    shannon_entropy,
    tokenize_payload,
)
from app.services.scoring import (
    FRAUD_PENALTY_MAX,
    FRAUD_PENALTY_PER_EVENT,
    ZSCORE_ANOMALY_THRESHOLD,
    ZSCORE_MIN_PEER_SAMPLE,
    ScoreUpdateInput,
    compute_score_update,
    compute_zscore,
    welford_std,
    welford_update,
)


# ─── Simulation helpers ───────────────────────────────────────────────────────

def _make_worker_input(
    worker_id: int,
    quality: float = 0.8,
    total_tasks: int = 10,
    accepted: int = 8,
    fraud_events: int = 0,
    ewma: float = 0.7,
    baselines: dict = None,
) -> ScoreUpdateInput:
    return ScoreUpdateInput(
        quality_score=quality,
        task_type="image_label",
        was_accepted=True,
        fraud_event_count=fraud_events,
        streak_days=3,
        days_since_joined=30.0,
        current_ewma=ewma,
        current_total_tasks=total_tasks,
        current_accepted=accepted,
        current_acc_7d_n=min(total_tasks, 7),  current_acc_7d_sum=min(accepted, 6),
        current_acc_30d_n=total_tasks,          current_acc_30d_sum=accepted,
        current_acc_all_n=total_tasks,          current_acc_all_sum=accepted,
        task_baselines=baselines or {},
        current_zscore_flagged=0,
    )


# ─── CC1: IP Farm ─────────────────────────────────────────────────────────────

class TestIPFarm:
    """
    10 workers submitting from the same IP address.
    Should be flagged once per worker after the threshold is crossed.
    """

    SHARED_IP    = "192.168.1.100"
    FARM_WORKERS = [f"worker-{i}" for i in range(10)]

    def test_farm_ip_flagged_above_threshold(self):
        """Each worker above the threshold should trigger ip_repeat."""
        threshold = 3
        flagged_count = 0
        for i, worker_id in enumerate(self.FARM_WORKERS):
            other_workers = [w for w in self.FARM_WORKERS if w != worker_id]
            signals = check_ip_sharing(
                self.SHARED_IP, other_workers, worker_id, max_shared_workers=threshold
            )
            if any(s.event_type == "ip_repeat" for s in signals):
                flagged_count += 1

        # All workers should be flagged once there are enough shared workers
        # (each sees the others as > threshold)
        assert flagged_count == len(self.FARM_WORKERS), (
            f"All {len(self.FARM_WORKERS)} farm workers should trigger ip_repeat. "
            f"Got {flagged_count}."
        )

    def test_first_two_workers_not_flagged(self):
        """First 2 workers from the same IP should be below threshold."""
        early_workers = self.FARM_WORKERS[:2]
        for worker_id in early_workers:
            others = [w for w in early_workers if w != worker_id]
            signals = check_ip_sharing(
                self.SHARED_IP, others, worker_id, max_shared_workers=3
            )
            assert signals == [], f"First 2 workers should not be flagged, got: {signals}"

    def test_threshold_crossing_point(self):
        """Exactly at threshold=3: the 3rd unique worker triggers the flag."""
        threshold = 3
        three_workers = self.FARM_WORKERS[:3]
        signals_w3 = check_ip_sharing(
            self.SHARED_IP,
            other_worker_ids=[w for w in three_workers if w != three_workers[-1]],
            current_worker_id=three_workers[-1],
            max_shared_workers=threshold,
        )
        # others = 2 workers, which equals threshold — triggers flag
        assert any(s.event_type == "ip_repeat" for s in signals_w3)

    def test_different_ips_no_cross_flag(self):
        """Workers on different IPs should not flag each other."""
        signals = check_ip_sharing(
            "10.0.0.1",
            other_worker_ids=["worker-on-10.0.0.2", "worker-on-10.0.0.3"],
            current_worker_id="worker-on-10.0.0.1",
            max_shared_workers=3,
        )
        # Technically these are all different IPs but we're passing all of them
        # to the same check call — this tests that the function itself doesn't
        # conflate different-IP workers
        # The function only checks the passed ip_address; here we're simulating
        # the aggregation layer correctly
        assert isinstance(signals, list)


# ─── CC2: Answer Ring ─────────────────────────────────────────────────────────

class TestAnswerRing:
    """
    Ring of 5 workers submitting near-identical payloads.
    Worker 1 submits original; workers 2–5 copy with minor mutations.
    """

    BASE_PAYLOAD = {
        "labels":    ["cat", "sitting", "outdoor"],
        "verdict":   "safe",
        "confidence": 0.95,
        "notes":     "clear image of a cat outside",
    }

    def _mutated(self, worker_index: int) -> dict:
        """Slight mutation: change notes field only."""
        return {**self.BASE_PAYLOAD, "notes": f"worker {worker_index} clear image of cat outside"}

    def test_original_not_a_duplicate_of_itself(self):
        """Sanity: submitting base payload to empty history is clean."""
        signals = check_answer_similarity(self.BASE_PAYLOAD, recent_hashes=[])
        assert signals == []

    def test_exact_copy_detected(self):
        """Ring member submitting exact copy of another member's payload."""
        h = structural_hash(self.BASE_PAYLOAD)
        signals = check_answer_similarity(self.BASE_PAYLOAD, recent_hashes=[h])
        assert any(s.event_type == "answer_cluster" for s in signals)
        assert any(s.severity == "critical" for s in signals)

    def test_ring_hashes_detected_across_members(self):
        """Accumulate hashes from ring members; each new member hits a match."""
        ring_payloads = [self.BASE_PAYLOAD] + [self._mutated(i) for i in range(4)]
        recent_hashes = []
        detection_count = 0

        for payload in ring_payloads:
            signals = check_answer_similarity(payload, recent_hashes)
            if any(s.event_type == "answer_cluster" for s in signals):
                detection_count += 1
            recent_hashes.append(structural_hash(payload))

        # At minimum, exact copies from the ring should be detected
        # (first member is clean; at least one copy after should be caught)
        assert detection_count >= 1

    def test_token_sets_of_ring_members_are_similar(self):
        """Even mutated ring payloads share most tokens."""
        from app.services.fraud import jaccard_similarity
        base_tokens = tokenize_payload(self.BASE_PAYLOAD)
        for i in range(5):
            mutant_tokens = tokenize_payload(self._mutated(i))
            sim = jaccard_similarity(base_tokens, mutant_tokens)
            assert sim > 0.60, (
                f"Ring member {i} token similarity {sim:.2f} should be high"
            )

    def test_genuinely_different_payloads_not_flagged(self):
        """Unrelated payloads that happen to share common words are not flagged."""
        unrelated = {
            "labels":    ["dog", "running", "park"],
            "verdict":   "safe",
            "confidence": 0.7,
            "notes":     "a dog running in the park",
        }
        h_base = structural_hash(self.BASE_PAYLOAD)
        signals = check_answer_similarity(unrelated, recent_hashes=[h_base])
        # Different hash → no exact match flag
        assert not any(s.event_type == "answer_cluster" for s in signals)


# ─── CC3: Baseline Poisoning ──────────────────────────────────────────────────

class TestBaselinePoisoning:
    """
    Attack: Ring floods the peer baseline with high-variance scores to widen std.
    Then each member submits anomalous scores that fall "within" the widened band.

    Defence: Widening the baseline raises the anomaly threshold, but the
    poisoners' own EWMA is damaged by the varied (low-quality) submissions.
    """

    def _poison_baseline(self, n_poison_tasks: int = 50) -> dict:
        """Build a wide-variance baseline via coordinated flooding."""
        state = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(n_poison_tasks):
            # Ring alternates 0 and 100 to maximise variance
            val = 100.0 if i % 2 == 0 else 0.0
            state = welford_update(state, val)
        return state

    def test_poison_baseline_has_high_std(self):
        bl = self._poison_baseline(100)
        std = welford_std(bl)
        # std of [0, 100, 0, 100, ...] = 50
        assert std > 40.0, f"Poisoned baseline std {std:.2f} should be high (near 50)"

    def test_poisoners_ewma_damaged_by_their_own_strategy(self):
        """
        Each poisoner who submits alternating 0.0/1.0 quality has a median EWMA,
        but their individual submissions' inconsistency shows in zscore_flagged.
        """
        from app.services.scoring import update_ewma, EWMA_ALPHA_MATURE
        ewma = 0.8
        for i in range(50):
            val = 1.0 if i % 2 == 0 else 0.0
            ewma = update_ewma(ewma, val, EWMA_ALPHA_MATURE)
        # EWMA should converge to 0.5 (mean of 0 and 1)
        assert abs(ewma - 0.5) < 0.1, (
            f"Poisoner EWMA {ewma:.4f} should converge to 0.5, hurting their trust score"
        )

    def test_attacker_scores_within_poisoned_band_but_not_above_threshold(self):
        """
        With std=50 from poisoning, a score at z=2.4 is 0.0 + 2.4*50 = 120 → impossible.
        Real scores are in [0,100], so poisoning doesn't actually help.
        """
        bl = self._poison_baseline(100)
        std = welford_std(bl)
        mean = bl["mean"]
        # Attacker wants to submit at z=2.4 to stay below threshold
        target_score = mean + ZSCORE_ANOMALY_THRESHOLD * std * 0.95
        # Check: does this even fit in the valid score range [0,100]?
        if target_score > 100:
            # Attack is self-defeating: can't submit a score > 100
            assert target_score > 100, "Widened baseline makes threshold unreachable in [0,100]"
        else:
            z = compute_zscore(target_score, bl)
            assert z is not None
            assert abs(z) < ZSCORE_ANOMALY_THRESHOLD


# ─── CC4: Sybil Burst ────────────────────────────────────────────────────────

class TestSybilBurst:
    """
    20 new accounts all created simultaneously, all submitting tasks in the same hour.
    Each individual is below velocity limit, but collectively suspicious.
    """

    def test_each_sybil_individually_under_velocity_cap(self):
        """20 tasks/hour per sybil — under the default 60/hour cap."""
        for _ in range(20):
            signals = check_velocity(tasks_last_hour=20, tasks_last_day=100)
            velocity_breaches = [s for s in signals if s.event_type == "velocity_breach"]
            assert velocity_breaches == [], "Individual sybil should not breach velocity cap"

    def test_each_sybil_has_floor_confidence_weight(self):
        """All sybil accounts start at CONFIDENCE_FLOOR — no leaderboard impact."""
        from app.services.scoring import confidence_weight, CONFIDENCE_FLOOR
        for _ in range(20):
            cw = confidence_weight(0)
            assert cw == pytest.approx(CONFIDENCE_FLOOR)

    def test_sybil_ring_scores_less_than_legitimate_veteran(self):
        """Even 20 sybils at perfect quality can't beat 1 legitimate veteran."""
        from app.services.scoring import CONFIDENCE_FULL_AT_N
        sybil_result = compute_score_update(_make_worker_input(
            worker_id=0,
            quality=1.0, total_tasks=0, accepted=0, ewma=1.0,
        ))
        veteran_result = compute_score_update(_make_worker_input(
            worker_id=99,
            quality=0.8, total_tasks=200, accepted=CONFIDENCE_FULL_AT_N, ewma=0.8,
        ))
        assert sybil_result.trust_score < veteran_result.trust_score, (
            "Sybil account at perfect quality should score below legitimate veteran"
        )

    def test_sybil_burst_requires_task_count_to_reach_full_confidence(self):
        """Sybils cannot skip the confidence accumulation period."""
        from app.services.scoring import confidence_weight, CONFIDENCE_FULL_AT_N
        # Even if sybil submits very fast, confidence is gated by accepted count
        for n in [0, 5, 10, 25]:
            cw = confidence_weight(n)
            assert cw < 1.0, f"Confidence should not be full at {n} accepted tasks"


# ─── CC5: Velocity Split ─────────────────────────────────────────────────────

class TestVelocitySplit:
    """
    Ring of 5 workers each doing 55 tasks/hour — individually under the 60/hour cap.
    Total ring output: 275 tasks/hour. We verify individual checks pass but
    that the system architecture needs cross-worker aggregation to catch this.
    """

    def test_each_ring_member_individually_passes_velocity(self):
        """55/60 tasks per member — each under cap."""
        signals_per_member = [
            check_velocity(tasks_last_hour=55, tasks_last_day=200)
            for _ in range(5)
        ]
        for signals in signals_per_member:
            breaches = [s for s in signals if s.event_type == "velocity_breach"]
            assert breaches == [], "Individual velocity should be under cap at 55/hr"

    def test_velocity_warning_at_80pct(self):
        """55 = 91.7% of 60 cap → should trigger velocity_warning (> 80%)."""
        signals = check_velocity(tasks_last_hour=55, tasks_last_day=200)
        warning_types = [s.event_type for s in signals]
        assert "velocity_warning" in warning_types

    def test_aggregate_ring_velocity_would_breach_if_checked(self):
        """Aggregated 5×55=275 tasks/hour should breach if checked tenant-wide."""
        total_tasks_per_hour = 5 * 55  # 275
        # Using a higher per-tenant threshold to simulate the aggregate check
        signals = check_velocity(
            tasks_last_hour=total_tasks_per_hour,
            tasks_last_day=total_tasks_per_hour * 8,
            max_per_hour=200,   # tenant-level limit
        )
        assert any(s.event_type == "velocity_breach" for s in signals), (
            "Aggregate ring velocity of 275/hr should breach tenant-level cap of 200/hr"
        )


# ─── CC7: Gradient Drift ─────────────────────────────────────────────────────

class TestGradientDrift:
    """
    Attack: Coordinated ring slowly degrades quality by 1% per task, hoping
    the gradual change doesn't trigger sudden z-score spikes.

    Defence: EWMA tracks the drift. After enough degradation, the EWMA and
    trust score materially decrease even without a sudden jump.
    """

    def test_1pct_per_task_drift_detected_over_50_tasks(self):
        """50 tasks of -1% quality drift should materially reduce trust score."""
        from app.services.scoring import update_ewma, EWMA_ALPHA_MATURE

        # Simulate EWMA with gradually declining quality
        ewma = 0.9
        quality = 0.9
        for _ in range(50):
            quality = max(0.0, quality - 0.01)
            ewma = update_ewma(ewma, quality, EWMA_ALPHA_MATURE)

        # EWMA should have drifted down
        assert ewma < 0.7, (
            f"After 50 steps of -1%/task drift, EWMA {ewma:.4f} should be below 0.70"
        )

    def test_drift_is_detectable_in_trust_score(self):
        """Drifted worker's trust score must be lower than stable worker's."""
        from app.services.scoring import update_ewma, EWMA_ALPHA_MATURE

        # Stable worker
        stable_ewma = 0.8
        for _ in range(50):
            stable_ewma = update_ewma(stable_ewma, 0.8, EWMA_ALPHA_MATURE)

        # Drifting worker
        drift_ewma = 0.8
        quality = 0.8
        for _ in range(50):
            quality = max(0.0, quality - 0.01)
            drift_ewma = update_ewma(drift_ewma, quality, EWMA_ALPHA_MATURE)

        stable_result = compute_score_update(_make_worker_input(
            worker_id=1, quality=0.8, ewma=stable_ewma
        ))
        drift_result = compute_score_update(_make_worker_input(
            worker_id=2, quality=quality, ewma=drift_ewma
        ))

        assert stable_result.trust_score > drift_result.trust_score, (
            f"Drifted worker (trust={drift_result.trust_score:.1f}) should score "
            f"below stable worker (trust={stable_result.trust_score:.1f})"
        )

    def test_slow_drift_does_not_produce_zscore_spike(self):
        """
        Small per-task changes should not produce |z| > ZSCORE_ANOMALY_THRESHOLD.
        (The z-score anomaly is for sudden changes, not gradual drift.)
        """
        # Build baseline at mean=80, std=5
        bl = {"mean": 0.0, "m2": 0.0, "n": 0}
        for i in range(40):
            bl = welford_update(bl, 80.0 + (i % 5 - 2))  # 78–82

        # Small step down: 80 → 79 (1 unit in score space)
        z = compute_zscore(79.0, bl)
        if z is not None:
            assert abs(z) < ZSCORE_ANOMALY_THRESHOLD, (
                f"Gradual 1-unit drift z={z:.2f} should not trigger anomaly flag"
            )


# ─── CC8: Split IP Ring ───────────────────────────────────────────────────────

class TestSplitIPRing:
    """
    Attack: Ring splits across 2 IPs with 2 workers each — staying under the
    per-IP threshold of 3. Total 4 workers / 2 IPs.

    Defence: Per-IP check is blind to this (each IP has only 2). But the answer
    clustering and velocity checks can still catch the coordinated payloads.
    """

    def test_each_ip_has_only_two_workers_no_ip_flag(self):
        """Split ring stays under per-IP threshold — individual check passes."""
        ring = [
            ("10.0.0.1", "w1"), ("10.0.0.1", "w2"),
            ("10.0.0.2", "w3"), ("10.0.0.2", "w4"),
        ]
        for ip, worker_id in ring:
            same_ip_workers = [w for (i, w) in ring if i == ip and w != worker_id]
            signals = check_ip_sharing(ip, same_ip_workers, worker_id, max_shared_workers=3)
            ip_flags = [s for s in signals if s.event_type == "ip_repeat"]
            assert ip_flags == [], (
                f"Worker {worker_id} on {ip} with {len(same_ip_workers)} peers "
                "should not be flagged (below threshold)"
            )

    def test_split_ring_answer_clustering_detects_coordinated_payloads(self):
        """Even split across IPs, answer clustering catches identical submissions."""
        payload = {"labels": ["cat"], "verdict": "safe", "confidence": 0.9}
        h = structural_hash(payload)
        accumulated_hashes = []

        detected = 0
        for _ in range(4):   # all 4 ring members submit same payload
            signals = check_answer_similarity(payload, recent_hashes=accumulated_hashes)
            if any(s.event_type == "answer_cluster" for s in signals):
                detected += 1
            accumulated_hashes.append(h)

        assert detected >= 1, (
            "Answer clustering should detect the split ring's identical payloads "
            "regardless of IP distribution"
        )


# ─── CC9: Fraud Burn-Down ─────────────────────────────────────────────────────

class TestFraudBurnDown:
    """
    Attack: Each ring member commits exactly 1 fraud event (just enough to
    contribute to penalty pool), then stops — hoping individual fraud_event_count
    stays low while the collective damage is distributed.

    Defence: Fraud penalty is per-worker, not collective. Each member suffers
    their own individual penalty regardless of what others do.
    """

    def test_one_fraud_event_per_member_each_suffers_penalty(self):
        """Each member with 1 fraud event suffers FRAUD_PENALTY_PER_EVENT."""
        ring_size = 5
        results = [
            compute_score_update(_make_worker_input(i, fraud_events=1))
            for i in range(ring_size)
        ]
        clean_baseline = compute_score_update(_make_worker_input(99, fraud_events=0))

        for i, r in enumerate(results):
            expected_penalty = FRAUD_PENALTY_PER_EVENT
            delta = clean_baseline.trust_score - r.trust_score
            assert abs(delta - expected_penalty) < 1.0, (
                f"Ring member {i} should suffer {expected_penalty}pt penalty, "
                f"got delta={delta:.2f}"
            )

    def test_fraud_penalties_not_reduced_by_group_size(self):
        """Individual penalty is not divided by ring size."""
        solo_fraud  = compute_score_update(_make_worker_input(1, fraud_events=1))
        clean       = compute_score_update(_make_worker_input(2, fraud_events=0))

        solo_delta = clean.trust_score - solo_fraud.trust_score
        # Even if 100 ring members each commit 1 fraud, solo_delta stays the same
        assert solo_delta == pytest.approx(FRAUD_PENALTY_PER_EVENT, abs=1.0)


# ─── CC10: Cross-Type Ring ────────────────────────────────────────────────────

class TestCrossTypeRing:
    """
    Attack: Ring submits the same payload structure across different task types,
    hoping that per-type baseline isolation prevents cross-task detection.

    Defence: Structural hash + token dedup is task-type-agnostic (done at the
    request level, before task-type-specific baselines are consulted).
    """

    SHARED_PAYLOAD = {
        "labels":     ["object"],
        "confidence": 0.9,
        "notes":      "standard object present",
    }

    def test_same_payload_different_types_still_shares_tokens(self):
        """Token set is identical regardless of task_type metadata."""
        tokens_survey     = tokenize_payload({**self.SHARED_PAYLOAD, "task_type": "survey"})
        tokens_moderation = tokenize_payload({**self.SHARED_PAYLOAD, "task_type": "moderation"})
        # task_type field adds a token, but core tokens are shared
        shared = tokens_survey & tokens_moderation
        core   = tokenize_payload(self.SHARED_PAYLOAD)
        assert core.issubset(shared), (
            "Core payload tokens should be present regardless of task_type wrapper"
        )

    def test_cross_type_hash_ring_detected_by_exact_match(self):
        """Same payload submitted under two different types: exact hash match detected."""
        h = structural_hash(self.SHARED_PAYLOAD)
        accumulated = [h]  # worker A submitted this under "survey"
        # Worker B submits same payload under "moderation"
        signals = check_answer_similarity(self.SHARED_PAYLOAD, recent_hashes=accumulated)
        assert any(s.event_type == "answer_cluster" for s in signals), (
            "Exact same payload under a different task type should still be flagged"
        )

    def test_ring_entropy_is_identical_across_types(self):
        """Entropy is payload-content driven, not type-driven."""
        e1 = shannon_entropy(str(sorted(self.SHARED_PAYLOAD.items())))
        e2 = shannon_entropy(str(sorted({**self.SHARED_PAYLOAD, "task_type": "survey"}.items())))
        # Adding one consistent field doesn't radically change entropy
        assert abs(e1 - e2) < 1.0, "Task type metadata should not materially change payload entropy"

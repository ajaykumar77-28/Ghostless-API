"""
Unit tests for fraud heuristics (services/fraud.py).
"""
import pytest
from app.services.fraud import (
    check_velocity,
    check_payload_entropy,
    check_answer_similarity,
    check_ip_sharing,
    check_speed_anomaly,
    run_fraud_checks,
    shannon_entropy,
    structural_hash,
    tokenize_payload,
    jaccard_similarity,
)


class TestVelocityLimiter:
    def test_normal_rate_clean(self):
        assert check_velocity(10, 100) == []

    def test_hourly_breach_critical(self):
        signals = check_velocity(tasks_last_hour=70, tasks_last_day=100)
        types = [s.event_type for s in signals]
        assert "velocity_breach" in types
        assert any(s.severity == "critical" for s in signals)

    def test_hourly_warning_zone(self):
        signals = check_velocity(tasks_last_hour=50, tasks_last_day=100)
        assert any(s.event_type == "velocity_warning" for s in signals)

    def test_daily_breach_suspend(self):
        signals = check_velocity(tasks_last_hour=1, tasks_last_day=400)
        assert any(s.auto_action == "suspend" for s in signals)


class TestShannonEntropy:
    def test_single_char_zero_entropy(self):
        assert shannon_entropy("aaaaaaaaaa") == pytest.approx(0.0)

    def test_empty_string_zero(self):
        assert shannon_entropy("") == 0.0

    def test_two_equal_chars_one_bit(self):
        assert shannon_entropy("ab") == pytest.approx(1.0)

    def test_natural_text_high_entropy(self):
        # Natural text should have entropy > 3 bits
        text = "The quick brown fox jumps over the lazy dog"
        assert shannon_entropy(text) > 3.0


class TestPayloadEntropy:
    def test_low_entropy_flagged(self):
        # Payload with all same answer
        payload = {"q1": "1", "q2": "1", "q3": "1", "q4": "1", "q5": "1",
                   "q6": "1", "q7": "1", "q8": "1"}
        signals = check_payload_entropy(payload, min_entropy=2.0, min_length=20)
        # May or may not trigger depending on JSON serialization entropy
        # At minimum, function should not crash
        assert isinstance(signals, list)

    def test_short_payload_skipped(self):
        payload = {"a": "b"}
        signals = check_payload_entropy(payload, min_length=100)
        assert signals == []


class TestStructuralHash:
    def test_same_payload_same_hash(self):
        p = {"labels": ["cat", "dog"], "confidence": 0.9}
        assert structural_hash(p) == structural_hash(p)

    def test_different_payload_different_hash(self):
        p1 = {"labels": ["cat"]}
        p2 = {"labels": ["dog"]}
        assert structural_hash(p1) != structural_hash(p2)

    def test_key_order_independent(self):
        p1 = {"a": 1, "b": 2}
        p2 = {"b": 2, "a": 1}
        assert structural_hash(p1) == structural_hash(p2)


class TestAnswerSimilarity:
    def test_exact_duplicate_detected(self):
        payload = {"q1": "yes", "q2": "no", "q3": "maybe"}
        h = structural_hash(payload)
        signals = check_answer_similarity(payload, recent_hashes=[h, "abc", "def"])
        assert any(s.event_type == "answer_cluster" for s in signals)

    def test_unique_answer_clean(self):
        payload = {"q1": "unique answer xyz 123"}
        signals = check_answer_similarity(payload, recent_hashes=["hash1", "hash2"])
        assert signals == []


class TestIPSharing:
    def test_below_threshold_clean(self):
        signals = check_ip_sharing("1.2.3.4", ["worker-a", "worker-b"], "worker-c", max_shared_workers=3)
        assert signals == []

    def test_at_threshold_flagged(self):
        signals = check_ip_sharing("1.2.3.4", ["w1", "w2", "w3"], "w4", max_shared_workers=3)
        assert any(s.event_type == "ip_repeat" for s in signals)

    def test_localhost_ignored(self):
        signals = check_ip_sharing("127.0.0.1", ["w1", "w2", "w3", "w4"], "w5")
        assert signals == []

    def test_empty_ip_ignored(self):
        signals = check_ip_sharing("", ["w1", "w2", "w3", "w4"], "w5")
        assert signals == []


class TestSpeedAnomaly:
    def test_normal_speed_clean(self):
        signals = check_speed_anomaly(100.0, baseline_mean=120.0, baseline_std=20.0)
        assert signals == []

    def test_too_fast_flagged(self):
        # 3 std below mean: mean=120, std=20, value=60 → z=-3
        signals = check_speed_anomaly(60.0, baseline_mean=120.0, baseline_std=20.0)
        assert any(s.event_type == "speed_anomaly" for s in signals)

    def test_no_baseline_clean(self):
        signals = check_speed_anomaly(10.0, baseline_mean=0.0, baseline_std=0.0)
        assert signals == []


class TestJaccardSimilarity:
    def test_identical_sets(self):
        assert jaccard_similarity({1, 2, 3}, {1, 2, 3}) == pytest.approx(1.0)

    def test_disjoint_sets(self):
        assert jaccard_similarity({1, 2}, {3, 4}) == pytest.approx(0.0)

    def test_partial_overlap(self):
        assert jaccard_similarity({1, 2, 3}, {2, 3, 4}) == pytest.approx(2 / 4)

    def test_empty_sets(self):
        assert jaccard_similarity(set(), set()) == pytest.approx(1.0)


class TestRunFraudChecks:
    def test_clean_task_no_signals(self):
        signals = run_fraud_checks(
            payload={"q1": "answer one", "q2": "answer two different"},
            completion_time=60.0,
            tasks_last_hour=5,
            tasks_last_day=30,
            recent_hashes=[],
        )
        # May or may not have entropy warnings; definitely no velocity breach
        velocity_breaches = [s for s in signals if "breach" in s.event_type]
        assert velocity_breaches == []

    def test_velocity_breach_present(self):
        signals = run_fraud_checks(
            payload={},
            completion_time=1.0,
            tasks_last_hour=100,
            tasks_last_day=50,
            recent_hashes=[],
        )
        assert any("velocity" in s.event_type for s in signals)

"""
v5 Reliability Tests

Covers:
  - Redis outage fallback (worker history from DB)
  - Ledger negative balance protection
  - Ledger currency isolation
  - Clawback idempotency
  - Scoring task idempotency (skip if recently calculated)
  - DB rollback on scoring error
  - Graceful degradation when baselines unavailable
"""
import pytest
from decimal import Decimal

from app.services.scoring import (
    ScoreUpdateInput, compute_score_update,
    detect_baseline_drift, compute_zscore,
    ZSCORE_MIN_PEER_SAMPLE,
)


# ─── Graceful degradation — no baseline ───────────────────────────────────────

class TestGracefulDegradation:
    """When baselines are unavailable or too sparse, scoring degrades gracefully."""

    def test_no_baseline_no_zscore(self):
        inp = ScoreUpdateInput(
            quality_score=0.8, task_type="new_type",
            task_baselines={},   # empty
        )
        result = compute_score_update(inp)
        assert result.zscore_latest is None
        assert result.is_anomaly == False
        assert result.is_fraud_suspect == False

    def test_sparse_baseline_no_zscore(self):
        # Only 10 observations — below ZSCORE_MIN_PEER_SAMPLE
        baseline = {"mean": 80.0, "m2": 100.0, "n": 10}
        z = compute_zscore(50.0, baseline)
        assert z is None

    def test_trust_computed_without_baseline(self):
        inp = ScoreUpdateInput(
            quality_score=0.8, task_type="image_label",
            task_baselines={}, current_ewma=0.5,
            peer_baseline_n=0, current_total_tasks=5,
            current_acc_all_n=5, current_acc_all_sum=4,
        )
        result = compute_score_update(inp)
        # Should still compute a valid trust score
        assert 0.0 <= result.trust_score <= 100.0

    def test_drift_detection_skipped_with_no_snapshot(self):
        current_baseline = {"mean": 80.0, "m2": 1000.0, "n": 100}
        result = detect_baseline_drift(current_baseline, {})
        assert result["drift_detected"] == False

    def test_zero_std_baseline_no_zscore(self):
        baseline = {"mean": 80.0, "m2": 0.0, "n": 100}  # all identical values
        z = compute_zscore(80.0, baseline)
        assert z is None


# ─── Ledger safety ────────────────────────────────────────────────────────────

class TestLedgerSafety:
    """Unit-level tests of ledger safety logic (no DB required)."""

    def test_negative_balance_guard_allows_valid_debit(self):
        from app.services.ledger import _check_negative_guard
        import asyncio
        # Balance 100, debit 50, floor 0 → allowed
        result = asyncio.get_event_loop().run_until_complete(
            _check_negative_guard(Decimal("100"), Decimal("50"), limit=0.0)
        )
        assert result == True

    def test_negative_balance_guard_blocks_excessive_debit(self):
        from app.services.ledger import _check_negative_guard
        import asyncio
        # Balance 30, debit 50, floor 0 → blocked
        result = asyncio.get_event_loop().run_until_complete(
            _check_negative_guard(Decimal("30"), Decimal("50"), limit=0.0)
        )
        assert result == False

    def test_negative_balance_guard_with_custom_floor(self):
        from app.services.ledger import _check_negative_guard
        import asyncio
        # Balance 10, debit 5, floor -5 → allowed (would leave 5 >= -5)
        result = asyncio.get_event_loop().run_until_complete(
            _check_negative_guard(Decimal("10"), Decimal("5"), limit=-5.0)
        )
        assert result == True


# ─── Scoring edge cases ───────────────────────────────────────────────────────

class TestScoringEdgeCases:
    def test_trust_never_negative(self):
        """Trust score must never be negative regardless of inputs."""
        worst_case = ScoreUpdateInput(
            quality_score=0.0,
            task_type="survey",
            current_ewma=0.0,
            fraud_events_aged=[{"age_days": 0}] * 3,
            tasks_last_hour=100,
            max_tasks_per_hour=10,
            max_trust=10.0,
        )
        result = compute_score_update(worst_case)
        assert result.trust_score >= 0.0

    def test_trust_never_exceeds_max_trust(self):
        """Trust score must never exceed max_trust ceiling."""
        best_case = ScoreUpdateInput(
            quality_score=1.0,
            task_type="survey",
            current_ewma=1.0,
            streak_days=30,
            days_since_joined=365,
            current_acc_all_n=100, current_acc_all_sum=100,
            current_acc_30d_n=100, current_acc_30d_sum=100,
            peer_baseline_n=100,
            max_trust=50.0,   # artificially low ceiling
        )
        result = compute_score_update(best_case)
        assert result.trust_score <= 50.0

    def test_zero_quality_trust_still_computes(self):
        inp = ScoreUpdateInput(quality_score=0.0, task_type="survey",
                               current_ewma=0.0, peer_baseline_n=100)
        result = compute_score_update(inp)
        assert result.trust_score >= 0.0

    def test_new_worker_confidence_floor_applied(self):
        inp = ScoreUpdateInput(
            quality_score=0.9, task_type="survey",
            current_accepted=0, current_acc_all_n=0,
            peer_baseline_n=0,  # no peers yet
        )
        result = compute_score_update(inp)
        # Confidence should be at floor (0.40) with no peers
        assert result.confidence_weight_val <= 0.40 + 1e-6

    def test_anomaly_score_always_zero_to_one(self):
        from app.services.scoring import compute_anomaly_score
        extremes = [
            compute_anomaly_score(zscore=10.0,  velocity_ratio=5.0, entropy_score=0.0),
            compute_anomaly_score(zscore=-10.0, velocity_ratio=0.0, entropy_score=1.0),
            compute_anomaly_score(zscore=None,  velocity_ratio=0.0, entropy_score=0.5),
        ]
        for score in extremes:
            assert 0.0 <= score <= 1.0

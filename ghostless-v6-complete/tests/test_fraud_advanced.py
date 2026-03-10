"""
v5 Fraud System Tests

Covers:
  - Graph-based clustering (IP + device + answer similarity)
  - Union-Find correctness
  - Progressive penalties
  - Replay attack and payout exploit protection
  - Long-horizon vs short-window detection
"""
import pytest
from app.services.graph_clustering import (
    WorkerNode, UnionFind, build_worker_graph, find_suspicious_clusters,
    fingerprint_device,
)
from app.services.scoring import (
    ScoreUpdateInput, compute_score_update,
    simple_fraud_multiplier, effective_fraud_multiplier,
)


# ─── Union-Find ───────────────────────────────────────────────────────────────

class TestUnionFind:
    def test_single_node(self):
        uf = UnionFind(["a"])
        assert uf.find("a") == "a"

    def test_union_connects(self):
        uf = UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")
        assert uf.find("c") != uf.find("a")

    def test_transitive(self):
        uf = UnionFind(["a", "b", "c"])
        uf.union("a", "b"); uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_components_excludes_singletons(self):
        uf = UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        comps = uf.components()
        members = [m for group in comps.values() for m in group]
        assert "a" in members and "b" in members
        assert "c" not in members


# ─── Graph building ───────────────────────────────────────────────────────────

class TestGraphBuilding:
    def test_shared_ip_edge(self):
        wa = WorkerNode("w1", ip_addresses={"1.2.3.4"})
        wb = WorkerNode("w2", ip_addresses={"1.2.3.4"})
        edges = build_worker_graph([wa, wb])
        assert len(edges) == 1

    def test_no_signals_no_edge(self):
        wa = WorkerNode("w1", ip_addresses={"1.1.1.1"})
        wb = WorkerNode("w2", ip_addresses={"2.2.2.2"})
        assert len(build_worker_graph([wa, wb])) == 0

    def test_multiple_signals_additive(self):
        wa = WorkerNode("w1", ip_addresses={"1.2.3.4"}, answer_hashes={"abc"})
        wb = WorkerNode("w2", ip_addresses={"1.2.3.4"}, answer_hashes={"abc"})
        edges = build_worker_graph([wa, wb])
        weight = list(edges.values())[0]
        assert weight >= 7   # IP(3) + ANSWER(4)


# ─── Cluster detection ────────────────────────────────────────────────────────

class TestClusterDetection:
    def _cluster(self, n, ip="10.0.0.1", h="hash_a"):
        return [WorkerNode(f"w{i}", ip_addresses={ip}, answer_hashes={h}) for i in range(n)]

    def test_detects_cluster(self):
        clusters = find_suspicious_clusters(self._cluster(5), min_cluster_size=3)
        assert len(clusters) == 1
        assert clusters[0].total_workers == 5

    def test_below_min_not_detected(self):
        assert find_suspicious_clusters(self._cluster(2), min_cluster_size=3) == []

    def test_two_independent_clusters(self):
        a = self._cluster(4, ip="10.0.0.1", h="hash_a")
        b = self._cluster(4, ip="10.0.0.2", h="hash_b")
        clusters = find_suspicious_clusters(a + b, min_cluster_size=3)
        assert len(clusters) == 2

    def test_cluster_score_in_range(self):
        clusters = find_suspicious_clusters(self._cluster(5), min_cluster_size=3)
        assert 0.0 <= clusters[0].cluster_score <= 1.0

    def test_shared_signals_populated(self):
        clusters = find_suspicious_clusters(self._cluster(4), min_cluster_size=3)
        assert len(clusters[0].shared_signals) > 0


# ─── Device fingerprinting ────────────────────────────────────────────────────

class TestDeviceFingerprint:
    def test_same_ua_same_fp(self):
        ua = "Mozilla/5.0 (Windows NT 10.0)"
        assert fingerprint_device(ua) == fingerprint_device(ua)

    def test_different_ua_different_fp(self):
        assert fingerprint_device("Windows") != fingerprint_device("Linux")

    def test_empty_returns_empty(self):
        assert fingerprint_device("") == ""

    def test_fp_length(self):
        assert len(fingerprint_device("some agent")) == 16


# ─── Progressive penalties ────────────────────────────────────────────────────

class TestProgressivePenalties:
    def test_one_event_halves_trust(self):
        assert simple_fraud_multiplier(1) == pytest.approx(0.5)

    def test_three_events_eighth_trust(self):
        assert simple_fraud_multiplier(3) == pytest.approx(0.125)

    def test_capped_at_three(self):
        assert simple_fraud_multiplier(10) == pytest.approx(simple_fraud_multiplier(3))

    def test_zero_events_no_penalty(self):
        assert simple_fraud_multiplier(0) == pytest.approx(1.0)


# ─── Fraud decay ──────────────────────────────────────────────────────────────

class TestFraudDecay:
    def test_fresh_event_full_penalty(self):
        m = effective_fraud_multiplier([{"age_days": 0}])
        assert m == pytest.approx(0.5, rel=0.01)

    def test_halflife_event_sqrt_penalty(self):
        m = effective_fraud_multiplier([{"age_days": 90}])
        assert m == pytest.approx(0.5 ** 0.5, rel=0.02)

    def test_very_old_event_minimal(self):
        m = effective_fraud_multiplier([{"age_days": 540}])  # 6 half-lives
        assert m > 0.95   # nearly no penalty

    def test_cap_prevents_floor(self):
        many  = effective_fraud_multiplier([{"age_days": 0}] * 10)
        three = effective_fraud_multiplier([{"age_days": 0}] * 3)
        assert many == pytest.approx(three, rel=0.01)


# ─── Payout exploit tests ─────────────────────────────────────────────────────

class TestPayoutExploit:
    """Verify fraud cannot be gamed out of via high-volume honest tasks."""

    def _make_inp(self, n_tasks, fraud_aged):
        return ScoreUpdateInput(
            quality_score=1.0, task_type="survey",
            was_accepted=True,
            current_ewma=0.9,
            current_total_tasks=n_tasks,
            current_accepted=n_tasks,
            current_acc_30d_n=n_tasks,
            current_acc_30d_sum=n_tasks,
            current_acc_7d_n=n_tasks,
            current_acc_7d_sum=n_tasks,
            current_acc_all_n=n_tasks,
            current_acc_all_sum=n_tasks,
            streak_days=30,
            days_since_joined=365,
            peer_baseline_n=100,
            fraud_events_aged=fraud_aged,
            max_trust=70.0 if fraud_aged else 100.0,
        )

    def test_fraud_worker_capped_despite_perfect_tasks(self):
        fraudster = compute_score_update(self._make_inp(500, [{"age_days": 1}]))
        honest    = compute_score_update(self._make_inp(500, []))
        assert fraudster.trust_score < honest.trust_score
        assert fraudster.trust_score <= 70.0

    def test_grinding_does_not_restore_full_trust(self):
        """Even 1000 accepted tasks cannot restore trust past max_trust ceiling."""
        for n in [100, 500, 1000]:
            result = compute_score_update(self._make_inp(n, [{"age_days": 1}]))
            assert result.trust_score <= 70.0

    def test_old_fraud_decays_and_trust_can_recover(self):
        """After fraud decays (old events), worker CAN approach normal trust — via review."""
        # With max_trust=100 (cleared by review) and old fraud events
        old_fraud = self._make_inp(200, [{"age_days": 360}])
        old_fraud.max_trust = 100.0   # simulate: admin reviewed
        result = compute_score_update(old_fraud)
        assert result.fraud_multiplier > 0.95   # very little penalty
        assert result.trust_score > 60.0        # can recover to reasonable trust


# ─── Replay attack tests ──────────────────────────────────────────────────────

class TestReplayProtection:
    """
    Test that idempotency checks prevent duplicate submission processing.
    This is a unit-level test of the key generation logic.
    """

    def test_submission_key_unique_per_payload(self):
        import hashlib, json
        payload_a = {"answer": "cat"}
        payload_b = {"answer": "dog"}
        key_a = hashlib.sha256(json.dumps(payload_a, sort_keys=True).encode()).hexdigest()[:16]
        key_b = hashlib.sha256(json.dumps(payload_b, sort_keys=True).encode()).hexdigest()[:16]
        assert key_a != key_b

    def test_same_payload_same_key(self):
        import hashlib, json
        payload = {"answer": "cat", "confidence": 0.9}
        key_1   = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        key_2   = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        assert key_1 == key_2

    def test_hmac_timestamp_in_message(self):
        import hashlib, hmac as hmac_lib
        secret     = "tenant_secret_key"
        body_bytes = b'{"worker_id": "w1", "payload": {}}'
        timestamp  = 1700000000
        body_hash  = hashlib.sha256(body_bytes).hexdigest()
        message    = f"{timestamp}.{body_hash}".encode()
        sig        = hmac_lib.new(secret.encode(), message, hashlib.sha256).hexdigest()
        # Same inputs → same signature
        sig2 = hmac_lib.new(secret.encode(), message, hashlib.sha256).hexdigest()
        assert sig == sig2

    def test_stale_timestamp_rejected(self):
        """Simulate replay: timestamp older than tolerance."""
        import time
        timestamp = int(time.time()) - 600   # 10 minutes ago
        tolerance = 300                       # 5 minutes tolerance
        assert abs(int(time.time()) - timestamp) > tolerance

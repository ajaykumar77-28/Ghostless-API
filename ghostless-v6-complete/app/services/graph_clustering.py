"""
Ghostless API — Graph-Based Worker Clustering (v5)

Builds a similarity graph across workers sharing:
  - IP addresses
  - Device fingerprints (user-agent hash)
  - Structural answer hashes
  - High Jaccard similarity on tokenized payloads

Runs on a configurable lookback window (default: 7 days).
Used by the long-horizon fraud clustering Celery task.

Algorithm:
  1. Build adjacency list: workers are edges; each shared signal = 1 edge weight
  2. Run Union-Find (disjoint set) to identify connected components
  3. Components with >= MIN_CLUSTER_SIZE workers and >= MIN_EDGE_DENSITY are suspects
  4. Each suspect cluster becomes a CoordinatedAttackEvent row

Output: list[ClusterResult]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional
import hashlib
import json


# ─── Config ───────────────────────────────────────────────────────────────────

MIN_CLUSTER_SIZE   = 3       # minimum workers to constitute a cluster
MIN_SHARED_SIGNALS = 2       # minimum shared signals between any pair
EDGE_WEIGHT_IP     = 3       # IP sharing is strongest signal
EDGE_WEIGHT_DEVICE = 2       # same device fingerprint
EDGE_WEIGHT_ANSWER = 4       # exact same structural answer hash
EDGE_WEIGHT_SIMILAR= 1       # Jaccard similarity above threshold


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class WorkerNode:
    worker_id:   str
    ip_addresses: Set[str] = field(default_factory=set)
    device_hashes: Set[str] = field(default_factory=set)
    answer_hashes: Set[str] = field(default_factory=set)
    token_sets:   List[Set[str]] = field(default_factory=list)   # per-submission tokens


@dataclass
class ClusterResult:
    worker_ids:      List[str]
    total_workers:   int
    shared_signals:  List[dict]    # [{type, value, workers_sharing}, ...]
    cluster_score:   float         # 0–1 suspicion score
    detection_type:  str = "long_horizon"


# ─── Union-Find (Disjoint Set Union) ─────────────────────────────────────────

class UnionFind:
    def __init__(self, nodes: List[str]):
        self.parent = {n: n for n in nodes}
        self.rank   = {n: 0 for n in nodes}

    def find(self, x: str) -> str:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])   # path compression
        return self.parent[x]

    def union(self, x: str, y: str):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        # Union by rank
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def components(self) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for node in self.parent:
            root = self.find(node)
            groups.setdefault(root, []).append(node)
        return {k: v for k, v in groups.items() if len(v) >= 2}


# ─── Graph builder ────────────────────────────────────────────────────────────

def build_worker_graph(workers: List[WorkerNode]) -> Dict[Tuple[str, str], int]:
    """
    Build an adjacency dict: {(worker_a, worker_b): edge_weight}
    where edge_weight is the total shared signal strength.
    """
    edges: Dict[Tuple[str, str], int] = {}

    def add_edge(a: str, b: str, weight: int):
        key = (min(a, b), max(a, b))
        edges[key] = edges.get(key, 0) + weight

    for i, wa in enumerate(workers):
        for wb in workers[i + 1:]:
            # Check shared IPs
            shared_ips = wa.ip_addresses & wb.ip_addresses
            if shared_ips:
                add_edge(wa.worker_id, wb.worker_id, EDGE_WEIGHT_IP * len(shared_ips))

            # Check shared device fingerprints
            shared_devices = wa.device_hashes & wb.device_hashes
            if shared_devices:
                add_edge(wa.worker_id, wb.worker_id, EDGE_WEIGHT_DEVICE * len(shared_devices))

            # Check exact answer hash overlap
            shared_answers = wa.answer_hashes & wb.answer_hashes
            if shared_answers:
                add_edge(wa.worker_id, wb.worker_id, EDGE_WEIGHT_ANSWER * len(shared_answers))

            # Check Jaccard similarity across all submission pairs
            sim_count = 0
            for ts_a in wa.token_sets:
                for ts_b in wb.token_sets:
                    if ts_a and ts_b:
                        union = ts_a | ts_b
                        inter = ts_a & ts_b
                        j = len(inter) / len(union) if union else 0
                        if j >= 0.92:
                            sim_count += 1
            if sim_count > 0:
                add_edge(wa.worker_id, wb.worker_id, EDGE_WEIGHT_SIMILAR * sim_count)

    return edges


# ─── Cluster detector ─────────────────────────────────────────────────────────

def find_suspicious_clusters(
    workers: List[WorkerNode],
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    min_shared_signals: int = MIN_SHARED_SIGNALS,
) -> List[ClusterResult]:
    """
    Main entry point. Returns suspect clusters across the worker population.
    """
    if len(workers) < min_cluster_size:
        return []

    edges = build_worker_graph(workers)

    # Keep only edges with enough signal strength
    strong_edges = {pair: w for pair, w in edges.items() if w >= min_shared_signals}

    if not strong_edges:
        return []

    # Build connected components via Union-Find
    uf = UnionFind([w.worker_id for w in workers])
    for (a, b) in strong_edges:
        uf.union(a, b)

    components = uf.components()

    results = []
    worker_map = {w.worker_id: w for w in workers}

    for root, member_ids in components.items():
        if len(member_ids) < min_cluster_size:
            continue

        cluster_workers = [worker_map[wid] for wid in member_ids if wid in worker_map]

        # Gather shared signals for this cluster
        shared_signals = []

        # Shared IPs
        all_ips: Dict[str, Set[str]] = {}
        for w in cluster_workers:
            for ip in w.ip_addresses:
                all_ips.setdefault(ip, set()).add(w.worker_id)
        for ip, sharers in all_ips.items():
            if len(sharers) >= 2:
                shared_signals.append({
                    "type": "ip", "value": ip[:8] + "...",
                    "workers_sharing": len(sharers),
                })

        # Shared answer hashes
        all_hashes: Dict[str, Set[str]] = {}
        for w in cluster_workers:
            for h in w.answer_hashes:
                all_hashes.setdefault(h, set()).add(w.worker_id)
        for h, sharers in all_hashes.items():
            if len(sharers) >= 2:
                shared_signals.append({
                    "type": "answer_hash", "value": h[:12] + "...",
                    "workers_sharing": len(sharers),
                })

        # Cluster score: ratio of actual edges to possible edges
        possible_edges = len(member_ids) * (len(member_ids) - 1) / 2
        actual_edges   = sum(
            1 for (a, b) in strong_edges
            if a in member_ids and b in member_ids
        )
        cluster_score = round(actual_edges / max(possible_edges, 1), 3)

        results.append(ClusterResult(
            worker_ids    = member_ids,
            total_workers = len(member_ids),
            shared_signals = shared_signals,
            cluster_score  = cluster_score,
        ))

    return sorted(results, key=lambda c: c.cluster_score, reverse=True)


# ─── Device fingerprint hashing ───────────────────────────────────────────────

def fingerprint_device(user_agent: str) -> str:
    """Hash a user-agent string into a short device fingerprint."""
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode()).hexdigest()[:16]

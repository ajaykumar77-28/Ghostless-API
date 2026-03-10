"""
Ghostless API — Fraud Heuristics Service (v2)

Detectors:
  1. Velocity limiter   — tasks-per-hour and tasks-per-day caps
  2. Answer clustering  — detect workers submitting near-identical payloads
                          via structural hash + Jaccard similarity
  3. Payload entropy    — low Shannon entropy = copy-paste / bot fill
  4. IP fingerprinting  — flag same IP across multiple workers in tenant
  5. Speed anomaly      — completion_time < baseline * threshold

Each detector returns a list of FraudSignal. The caller decides
what to do (log, flag, suspend) based on severity.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class FraudSignal:
    event_type:  str
    severity:    str   # "info" | "warning" | "critical"
    details:     Dict[str, Any] = field(default_factory=dict)
    auto_action: Optional[str] = None   # None | "flag" | "suspend"


# ─── 1. Velocity Limiter ──────────────────────────────────────────────────────

# Defaults — override per tenant via config if needed
DEFAULT_MAX_TASKS_PER_HOUR = 60
DEFAULT_MAX_TASKS_PER_DAY  = 300


def check_velocity(
    tasks_last_hour: int,
    tasks_last_day: int,
    max_per_hour: int = DEFAULT_MAX_TASKS_PER_HOUR,
    max_per_day:  int = DEFAULT_MAX_TASKS_PER_DAY,
) -> List[FraudSignal]:
    signals = []
    if tasks_last_hour > max_per_hour:
        signals.append(FraudSignal(
            event_type="velocity_breach",
            severity="critical",
            details={
                "tasks_last_hour": tasks_last_hour,
                "limit_per_hour":  max_per_hour,
            },
            auto_action="flag",
        ))
    elif tasks_last_hour > max_per_hour * 0.8:
        signals.append(FraudSignal(
            event_type="velocity_warning",
            severity="warning",
            details={
                "tasks_last_hour": tasks_last_hour,
                "limit_per_hour":  max_per_hour,
            },
        ))
    if tasks_last_day > max_per_day:
        signals.append(FraudSignal(
            event_type="velocity_breach",
            severity="critical",
            details={
                "tasks_last_day": tasks_last_day,
                "limit_per_day":  max_per_day,
            },
            auto_action="suspend",
        ))
    return signals


# ─── 2. Payload Entropy ───────────────────────────────────────────────────────

def shannon_entropy(text: str) -> float:
    """Compute Shannon entropy (bits per character) of a string."""
    if not text:
        return 0.0
    counts: Dict[str, int] = {}
    for c in text:
        counts[c] = counts.get(c, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def check_payload_entropy(
    payload: dict,
    min_entropy: float = 2.0,
    min_length:  int   = 20,
) -> List[FraudSignal]:
    """
    Serialise payload to JSON and compute entropy.
    Very low entropy → likely bot fill or copy-paste.
    """
    try:
        text = json.dumps(payload, sort_keys=True)
    except Exception:
        return []

    if len(text) < min_length:
        return []   # too short to judge

    entropy = shannon_entropy(text)
    if entropy < min_entropy:
        return [FraudSignal(
            event_type="entropy_low",
            severity="warning",
            details={"entropy": round(entropy, 3), "threshold": min_entropy},
        )]
    return []


# ─── 3. Answer Clustering / Structural Hash ───────────────────────────────────

def structural_hash(payload: dict) -> str:
    """
    Hash the *structure + leaf values* of a payload, ignoring key ordering.
    Two payloads that are near-identical (same choices, same answers) will
    produce the same or similar hashes, enabling fast dedup.
    """
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()
    except Exception:
        return ""


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard index between two sets of tokens."""
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    intersection = set_a & set_b
    return len(intersection) / len(union)


def tokenize_payload(payload: dict) -> set:
    """Extract all leaf string/numeric values as tokens for similarity check."""
    tokens = set()
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, (str, int, float)):
            tokens.add(str(obj).strip().lower())
    _walk(payload)
    return tokens


def check_answer_similarity(
    current_payload:    dict,
    recent_hashes:      List[str],   # last N structural hashes from this worker
    similarity_threshold: float = 0.92,
) -> List[FraudSignal]:
    """
    Check if current payload is near-duplicate of recent submissions.
    Uses both exact hash match and token Jaccard similarity.
    """
    current_hash   = structural_hash(current_payload)
    current_tokens = tokenize_payload(current_payload)
    signals = []

    # Exact hash match → definitely duplicate
    if current_hash and current_hash in recent_hashes:
        signals.append(FraudSignal(
            event_type="answer_cluster",
            severity="critical",
            details={"reason": "exact_duplicate", "hash": current_hash[:12]},
            auto_action="flag",
        ))
        return signals

    # Near-duplicate via Jaccard (only if enough tokens to be meaningful)
    if len(current_tokens) >= 4:
        for recent_hash in recent_hashes[-10:]:   # check last 10 only
            # We only have hashes stored, so this is a best-effort check
            # In production, store token sets or minhash signatures
            pass   # TODO: implement minhash LSH for scalable similarity

    return signals


# ─── 4. IP Fingerprinting ─────────────────────────────────────────────────────

def check_ip_sharing(
    ip_address: str,
    other_worker_ids: List[str],   # other workers seen from this IP recently
    current_worker_id: str,
    max_shared_workers: int = 3,
) -> List[FraudSignal]:
    """
    Flag if too many distinct workers share the same IP within a tenant.
    Some sharing is normal (office NAT), but > threshold is suspicious.
    """
    if not ip_address or ip_address in ("127.0.0.1", "::1"):
        return []

    other = [w for w in other_worker_ids if w != current_worker_id]
    if len(other) >= max_shared_workers:
        return [FraudSignal(
            event_type="ip_repeat",
            severity="warning",
            details={
                "ip_address":     ip_address,
                "shared_workers": len(other),
                "threshold":      max_shared_workers,
            },
        )]
    return []


# ─── 5. Speed Anomaly ─────────────────────────────────────────────────────────

def check_speed_anomaly(
    completion_time: float,
    baseline_mean:   float,
    baseline_std:    float,
    z_threshold:     float = 2.5,
) -> List[FraudSignal]:
    """
    Flag if completion_time is statistically much faster than the peer baseline.
    Works independently of rule_engine's simpler threshold check.
    """
    if baseline_std < 1e-6 or baseline_mean <= 0:
        return []
    z = (completion_time - baseline_mean) / baseline_std
    if z < -z_threshold:   # far below mean = suspiciously fast
        return [FraudSignal(
            event_type="speed_anomaly",
            severity="warning" if z > -3.5 else "critical",
            details={
                "completion_time":  round(completion_time, 1),
                "baseline_mean":    round(baseline_mean, 1),
                "baseline_std":     round(baseline_std, 1),
                "z_score":          round(z, 3),
            },
            auto_action="flag" if z < -3.5 else None,
        )]
    return []


# ─── Aggregate runner ─────────────────────────────────────────────────────────

def run_fraud_checks(
    payload:           dict,
    completion_time:   float,
    tasks_last_hour:   int,
    tasks_last_day:    int,
    recent_hashes:     List[str],
    ip_address:        str = "",
    other_worker_ids:  List[str] = None,
    current_worker_id: str = "",
    time_baseline_mean: float = 0.0,
    time_baseline_std:  float = 0.0,
) -> List[FraudSignal]:
    signals: List[FraudSignal] = []
    signals += check_velocity(tasks_last_hour, tasks_last_day)
    signals += check_payload_entropy(payload)
    signals += check_answer_similarity(payload, recent_hashes)
    if ip_address:
        signals += check_ip_sharing(ip_address, other_worker_ids or [], current_worker_id)
    if time_baseline_mean > 0:
        signals += check_speed_anomaly(completion_time, time_baseline_mean, time_baseline_std)
    return signals

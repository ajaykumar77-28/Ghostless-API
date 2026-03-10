"""
Ghostless Python SDK
====================

A fully-typed, async-first Python client for the Ghostless API.

Installation:
    pip install ghostless

Quick start:
    import asyncio
    from ghostless import AsyncGhostlessClient

    async def main():
        client = AsyncGhostlessClient(
            api_key="sk_live_gl_your_key",
            tenant_id="your-tenant-slug",
        )

        # Validate a task before accepting
        result = await client.validate(
            worker_id="worker_123",
            task_type="survey",
            payload={"responses": {"q1": "A", "q2": "B"}},
            completion_time=45.2,
        )

        print(result.decision)          # "accept" | "review" | "reject"
        print(result.trust_score)       # 78.4
        print(result.explanation)       # human-readable sentence
        print(result.risk_level)        # "low" | "medium" | "high" | "critical"

    asyncio.run(main())
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

try:
    import httpx
    _has_httpx = True
except ImportError:
    _has_httpx = False

try:
    import urllib.request
    _has_stdlib = True
except ImportError:
    _has_stdlib = False


# ─── Enums ────────────────────────────────────────────────────────────────────

class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class RiskLevel(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Tier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD   = "gold"
    ELITE  = "elite"


# ─── Response Models ──────────────────────────────────────────────────────────

@dataclass
class Warning:
    code:    str
    message: str
    severity: str   # "info" | "warning" | "error"
    field:   Optional[str] = None

    @classmethod
    def _from_dict(cls, d: dict) -> "Warning":
        return cls(code=d["code"], message=d["message"], severity=d["severity"], field=d.get("field"))


@dataclass
class ValidationResult:
    """
    Result of a pre-submission validation call.

    Business-friendly properties:
        decision        → "accept" | "review" | "reject"
        risk_level      → "low" | "medium" | "high" | "critical"
        explanation     → plain-English sentence about why
        should_submit   → bool shorthand (True = go ahead)
    """
    validation_id:    str
    worker_id:        str
    task_type:        str
    quality_score:    float         # 0.0–1.0 rule-engine score
    allow_submit:     bool
    warnings:         List[Warning]
    suggestions:      List[str]
    flags:            List[str]
    anomaly_score:    float         # 0.0–1.0 composite anomaly
    velocity_warning: bool
    shadow_banned:    bool
    processed_ms:     int
    _raw:             dict = field(repr=False, default_factory=dict)

    # ── Business-friendly computed props ──────────────────────────────────────

    @property
    def decision(self) -> Decision:
        """High-level submission decision."""
        if not self.allow_submit:
            return Decision.REJECT
        if self.anomaly_score > 0.6 or self.velocity_warning or len(self.flags) >= 2:
            return Decision.REVIEW
        return Decision.ACCEPT

    @property
    def risk_level(self) -> RiskLevel:
        """Risk classification based on combined signals."""
        if self.anomaly_score >= 0.75 or any(f in self.flags for f in ("SPEED_FLAG", "STRAIGHT_LINE")):
            return RiskLevel.CRITICAL
        if self.anomaly_score >= 0.5 or self.velocity_warning or len(self.flags) >= 2:
            return RiskLevel.HIGH
        if self.anomaly_score >= 0.25 or len(self.flags) >= 1:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @property
    def explanation(self) -> str:
        """Plain-English reason for the decision."""
        if self.shadow_banned:
            return "This worker is under review. Submissions are accepted but earnings are paused."
        if not self.allow_submit:
            errors = [w for w in self.warnings if w.severity == "error"]
            if errors:
                return f"Submission blocked: {errors[0].message}"
            return "Submission quality below threshold. Please review and resubmit."
        if self.decision == Decision.REVIEW:
            parts = []
            if self.velocity_warning:
                parts.append("high submission rate")
            if self.anomaly_score > 0.5:
                parts.append("anomalous patterns detected")
            if self.flags:
                parts.append(f"quality flags: {', '.join(self.flags[:2])}")
            return f"Submission flagged for review due to: {'; '.join(parts)}."
        return "Submission looks good. Quality within expected parameters."

    @property
    def should_submit(self) -> bool:
        """Simple bool: True if you should let this submission through."""
        return self.decision in (Decision.ACCEPT, Decision.REVIEW)

    @classmethod
    def _from_dict(cls, d: dict) -> "ValidationResult":
        return cls(
            validation_id    = d["validation_id"],
            worker_id        = d["worker_id"],
            task_type        = d["task_type"],
            quality_score    = d["quality_score"],
            allow_submit     = d["allow_submit"],
            warnings         = [Warning._from_dict(w) for w in d.get("warnings", [])],
            suggestions      = d.get("suggestions", []),
            flags            = d.get("flags", []),
            anomaly_score    = d.get("anomaly_score", 0.0),
            velocity_warning = d.get("velocity_warning", False),
            shadow_banned    = d.get("shadow_banned", False),
            processed_ms     = d.get("processed_ms", 0),
            _raw             = d,
        )

    def __str__(self) -> str:
        return (
            f"ValidationResult({self.decision.value.upper()} | "
            f"risk={self.risk_level.value} | quality={self.quality_score:.0%} | "
            f"anomaly={self.anomaly_score:.2f})\n"
            f"  → {self.explanation}"
        )


@dataclass
class WorkerScore:
    """Current trust score and metrics for a worker."""
    worker_id:          str
    external_id:        str
    tier:               Tier
    trust_score:        float       # 0–100
    accuracy_7d:        Optional[float]
    accuracy_30d:       Optional[float]
    accuracy_all:       Optional[float]
    total_tasks:        int
    accepted_tasks:     int
    streak_days:        int
    last_calculated_at: Optional[str]
    _raw:               dict = field(repr=False, default_factory=dict)

    @property
    def acceptance_rate(self) -> Optional[float]:
        if self.total_tasks == 0:
            return None
        return round(self.accepted_tasks / self.total_tasks, 3)

    @property
    def trust_label(self) -> str:
        """Human-readable trust tier label."""
        if self.trust_score >= 90: return "Elite"
        if self.trust_score >= 75: return "Trusted"
        if self.trust_score >= 60: return "Reliable"
        if self.trust_score >= 40: return "Building"
        return "New"

    @property
    def summary(self) -> str:
        return (
            f"Worker {self.external_id} — {self.trust_label} ({self.trust_score:.1f}/100) | "
            f"Tier: {self.tier.value.title()} | "
            f"Tasks: {self.total_tasks} | Acceptance: {(self.acceptance_rate or 0):.0%} | "
            f"Streak: {self.streak_days}d"
        )

    @classmethod
    def _from_dict(cls, d: dict) -> "WorkerScore":
        return cls(
            worker_id          = d["worker_id"],
            external_id        = d["external_id"],
            tier               = Tier(d.get("tier", "bronze")),
            trust_score        = d.get("trust_score", 50.0),
            accuracy_7d        = d.get("accuracy_7d"),
            accuracy_30d       = d.get("accuracy_30d"),
            accuracy_all       = d.get("accuracy_all"),
            total_tasks        = d.get("total_tasks", 0),
            accepted_tasks     = d.get("accepted_tasks", 0),
            streak_days        = d.get("streak_days", 0),
            last_calculated_at = d.get("last_calculated_at"),
            _raw               = d,
        )


@dataclass
class ScoreExplanation:
    """Full Bayesian score explanation with confidence intervals."""
    worker_id:          str
    trust_score:        float
    algorithm_version:  str
    ci_lower:           float       # 90% credible interval lower
    ci_upper:           float       # 90% credible interval upper
    ci_width:           float
    posterior_mean:     float
    total_tasks:        int
    lifecycle_stage:    str
    score_volatility:   Optional[float]
    components:         Dict[str, Any]
    narrative:          str         # ← plain English summary
    last_updated:       Optional[str]
    _raw:               dict = field(repr=False, default_factory=dict)

    @property
    def confidence_band(self) -> str:
        """Plain-English confidence description."""
        if self.ci_width < 0.15: return "high confidence"
        if self.ci_width < 0.30: return "moderate confidence"
        return "low confidence (more data needed)"

    @classmethod
    def _from_dict(cls, d: dict) -> "ScoreExplanation":
        ci = d.get("credible_interval", {})
        return cls(
            worker_id         = d["worker_id"],
            trust_score       = d["trust_score"],
            algorithm_version = d.get("algorithm_version", "unknown"),
            ci_lower          = ci.get("lower", 0.0),
            ci_upper          = ci.get("upper", 1.0),
            ci_width          = ci.get("width", 1.0),
            posterior_mean    = d.get("posterior_mean", 0.5),
            total_tasks       = d.get("total_tasks", 0),
            lifecycle_stage   = d.get("lifecycle_stage", "unknown"),
            score_volatility  = d.get("score_volatility"),
            components        = d.get("components", {}),
            narrative         = d.get("narrative", ""),
            last_updated      = d.get("last_updated"),
            _raw              = d,
        )


@dataclass
class WebhookConfig:
    url:    str
    events: List[str]
    secret: Optional[str] = None


# ─── Exceptions ───────────────────────────────────────────────────────────────

class GhostlessError(Exception):
    def __init__(self, message: str, status_code: int = 0, response: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response    = response or {}


class AuthenticationError(GhostlessError): pass
class RateLimitError(GhostlessError):      pass
class NotFoundError(GhostlessError):       pass
class ValidationError(GhostlessError):     pass
class ServerError(GhostlessError):         pass


# ─── HMAC signing ─────────────────────────────────────────────────────────────

def _sign_request(body: bytes, secret: str, timestamp: int) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message   = f"{timestamp}.{body_hash}".encode()
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# ─── Async Client ─────────────────────────────────────────────────────────────

class AsyncGhostlessClient:
    """
    Async client for the Ghostless API. Requires httpx.

        pip install ghostless[async]   # or: pip install httpx

    Usage:
        async with AsyncGhostlessClient("sk_live_gl_...", "my-tenant") as client:
            result = await client.validate(...)
    """

    def __init__(
        self,
        api_key:    str,
        tenant_id:  str,
        base_url:   str = "https://api.ghostless.io",
        timeout:    float = 10.0,
        hmac_secret: Optional[str] = None,
    ):
        if not _has_httpx:
            raise ImportError("httpx is required for AsyncGhostlessClient. pip install httpx")

        self.api_key     = api_key
        self.tenant_id   = tenant_id
        self.base_url    = base_url.rstrip("/")
        self.timeout     = timeout
        self.hmac_secret = hmac_secret
        self._client:    Optional[httpx.AsyncClient] = None

    def _headers(self, body: bytes = b"") -> dict:
        headers = {
            "X-API-Key":    self.api_key,
            "X-Tenant-ID":  self.tenant_id,
            "Content-Type": "application/json",
            "User-Agent":   "ghostless-python-sdk/6.0.0",
        }
        if self.hmac_secret:
            ts = int(time.time())
            headers["X-Signature-SHA256"] = _sign_request(body, self.hmac_secret, ts)
            headers["X-Timestamp"]        = str(ts)
        return headers

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/v1/", path.lstrip("/"))

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

        body = json.dumps(kwargs.pop("json", {})).encode() if "json" in kwargs else b""
        if body:
            kwargs["content"] = body

        headers = self._headers(body)
        resp = await self._client.request(method, self._url(path), headers=headers, **kwargs)

        if resp.status_code == 401: raise AuthenticationError("Invalid API key or tenant ID.", resp.status_code)
        if resp.status_code == 404: raise NotFoundError("Resource not found.", resp.status_code)
        if resp.status_code == 429: raise RateLimitError("Rate limit exceeded. Slow down.", resp.status_code)
        if resp.status_code >= 500: raise ServerError(f"Server error: {resp.status_code}", resp.status_code)
        if resp.status_code >= 400: raise GhostlessError(f"Request failed: {resp.text}", resp.status_code)

        return resp.json()

    async def __aenter__(self): return self
    async def __aexit__(self, *_): await self.close()

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Validation ────────────────────────────────────────────────────────────

    async def validate(
        self,
        worker_id:       str,
        task_type:       str,
        payload:         Dict[str, Any],
        completion_time: float,
        project_id:      Optional[str]  = None,
        difficulty:      float          = 1.0,
        idempotency_key: Optional[str]  = None,
    ) -> ValidationResult:
        """
        Pre-validate a task submission before accepting it on your platform.

        Returns a ValidationResult with:
          - decision     → "accept" | "review" | "reject"
          - risk_level   → "low" | "medium" | "high" | "critical"
          - explanation  → plain-English reason
          - suggestions  → actionable feedback to show the worker

        Example:
            result = await client.validate(
                worker_id="user_456",
                task_type="image_label",
                payload={"labels": ["cat", "dog"]},
                completion_time=23.5,
            )
            if result.should_submit:
                await your_platform.accept_task(task_id)
                print(result.explanation)
        """
        body = {
            "worker_id":       worker_id,
            "task_type":       task_type,
            "payload":         payload,
            "completion_time": completion_time,
            "difficulty":      difficulty,
        }
        if project_id:   body["project_id"]   = project_id

        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = await self._request("POST", "/validate/task", json=body)
        return ValidationResult._from_dict(data)

    # ── Scores ────────────────────────────────────────────────────────────────

    async def get_score(self, worker_id: str) -> WorkerScore:
        """Get a worker's current trust score and metrics."""
        data = await self._request("GET", f"/workers/{worker_id}/score")
        return WorkerScore._from_dict(data)

    async def get_explanation(self, worker_id: str) -> ScoreExplanation:
        """Get full Bayesian score explanation with confidence intervals and narrative."""
        data = await self._request("GET", f"/scores/{worker_id}/explain")
        return ScoreExplanation._from_dict(data)

    async def get_score_history(
        self, worker_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get trust score change history (paginated)."""
        data = await self._request("GET", f"/scores/{worker_id}/history?limit={limit}")
        return data.get("history", [])

    async def trigger_recalc(self, worker_id: str) -> Dict[str, Any]:
        """Trigger an immediate score recalculation for a worker."""
        return await self._request("POST", f"/workers/{worker_id}/score/recalc")

    async def get_leaderboard(
        self, limit: int = 50, tier: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get top workers for your tenant."""
        url = f"/workers/leaderboard?limit={limit}"
        if tier: url += f"&tier={tier}"
        return await self._request("GET", url)

    # ── Earnings ──────────────────────────────────────────────────────────────

    async def get_earnings(self, worker_id: str) -> Dict[str, Any]:
        """Get a worker's earnings summary."""
        return await self._request("GET", f"/earnings/{worker_id}")

    # ── Webhooks ──────────────────────────────────────────────────────────────

    async def configure_webhooks(
        self,
        url:    str,
        events: List[str],
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Configure webhook delivery for your tenant.

        Available events:
            task.validated, task.accepted, task.rejected,
            worker.promoted, worker.suspended, payout.sent,
            fraud.detected, score.updated

        Example:
            await client.configure_webhooks(
                url="https://yourapp.com/hooks/ghostless",
                events=["task.validated", "worker.promoted", "fraud.detected"],
                secret="your-webhook-signing-secret",
            )
        """
        body = {"webhook_url": url, "webhook_events": events}
        if secret: body["webhook_secret"] = secret
        return await self._request("PUT", "/tenants/webhook", json=body)

    @staticmethod
    def verify_webhook(
        payload: bytes,
        signature: str,
        secret: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """
        Verify a webhook delivery from Ghostless.

        Usage in your webhook handler:
            body = await request.body()
            sig  = request.headers["X-Ghostless-Signature"]
            if not GhostlessClient.verify_webhook(body, sig, "your-secret"):
                raise HTTPException(401)

        Returns True if valid, False if invalid or replayed.
        """
        if not signature.startswith("sha256="):
            return False
        try:
            expected = hmac.new(
                secret.encode(), payload, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(f"sha256={expected}", signature)
        except Exception:
            return False


# ─── Sync Client (thin wrapper for synchronous use) ───────────────────────────

class GhostlessClient:
    """
    Synchronous client for the Ghostless API.

    Wraps AsyncGhostlessClient using urllib for zero extra dependencies.
    For production async workloads, prefer AsyncGhostlessClient.

    Usage:
        client = GhostlessClient("sk_live_gl_...", "my-tenant")
        result = client.validate(worker_id="worker_1", ...)
        print(result.decision)
    """

    def __init__(
        self,
        api_key:   str,
        tenant_id: str,
        base_url:  str = "https://api.ghostless.io",
        timeout:   float = 10.0,
        hmac_secret: Optional[str] = None,
    ):
        self.api_key     = api_key
        self.tenant_id   = tenant_id
        self.base_url    = base_url.rstrip("/")
        self.timeout     = timeout
        self.hmac_secret = hmac_secret

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        import urllib.request, urllib.error
        url     = urljoin(self.base_url + "/v1/", path.lstrip("/"))
        payload = json.dumps(body or {}).encode()
        headers = {
            "X-API-Key":    self.api_key,
            "X-Tenant-ID":  self.tenant_id,
            "Content-Type": "application/json",
            "User-Agent":   "ghostless-python-sdk/6.0.0",
        }
        if self.hmac_secret:
            ts = int(time.time())
            headers["X-Signature-SHA256"] = _sign_request(payload, self.hmac_secret, ts)
            headers["X-Timestamp"]        = str(ts)

        req = urllib.request.Request(
            url,
            data    = payload if method != "GET" else None,
            headers = headers,
            method  = method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            code = e.code
            body_text = e.read().decode()
            if code == 401: raise AuthenticationError("Invalid API key", code)
            if code == 404: raise NotFoundError("Not found", code)
            if code == 429: raise RateLimitError("Rate limited", code)
            raise GhostlessError(body_text, code)

    def validate(self, worker_id: str, task_type: str, payload: dict,
                 completion_time: float, difficulty: float = 1.0,
                 project_id: str = None) -> ValidationResult:
        body = {
            "worker_id": worker_id, "task_type": task_type,
            "payload": payload, "completion_time": completion_time,
            "difficulty": difficulty,
        }
        if project_id: body["project_id"] = project_id
        return ValidationResult._from_dict(self._request("POST", "/validate/task", body))

    def get_score(self, worker_id: str) -> WorkerScore:
        return WorkerScore._from_dict(self._request("GET", f"/workers/{worker_id}/score"))

    def get_explanation(self, worker_id: str) -> ScoreExplanation:
        return ScoreExplanation._from_dict(self._request("GET", f"/scores/{worker_id}/explain"))

    verify_webhook = staticmethod(AsyncGhostlessClient.verify_webhook)

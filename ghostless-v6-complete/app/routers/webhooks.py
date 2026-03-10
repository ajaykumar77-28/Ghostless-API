"""
Ghostless API — Enhanced Webhook System (v6)

What's new over v5:
  - Full event catalog with schema documentation
  - Subscription management per event type
  - Delivery status API (query any webhook's delivery history)
  - Dead-letter replay endpoint (retry failed deliveries without reprocessing)
  - Tenant-level event filtering
  - Batch event emission (single DB flush, multiple webhook dispatches)
  - Business-friendly event payloads with enriched context

Event catalog:
  task.validated      — every validate/task call
  task.accepted       — task graded as accepted
  task.rejected       — task graded as rejected
  worker.promoted     — tier promotion (bronze→silver etc.)
  worker.suspended    — worker suspended (velocity or fraud)
  worker.score_update — trust score changed by >= 5 points
  fraud.detected      — new fraud event created
  payout.sent         — payout confirmed and sent
  payout.clawback     — earnings clawed back after fraud review
  appeal.submitted    — worker submitted an appeal
  appeal.resolved     — admin resolved an appeal
"""
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, HttpUrl, Field
from sqlalchemy import select, desc, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, require_auth
from app.middleware.rbac import require_permission
from app.models.models import WebhookLog, WebhookDeadLetter, Tenant
from app.tasks.webhooks import dispatch_webhook, WEBHOOK_EVENTS

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# ─── Full event catalog ───────────────────────────────────────────────────────

EVENT_CATALOG: Dict[str, Dict[str, Any]] = {
    "task.validated": {
        "description": "Fired after every validate/task call, regardless of outcome.",
        "payload_fields": ["worker_id", "task_type", "quality_score", "allow_submit",
                           "flags", "anomaly_score", "decision", "risk_level"],
        "frequency": "high",
    },
    "task.accepted": {
        "description": "Fired when a task is graded as accepted.",
        "payload_fields": ["worker_id", "task_id", "task_type", "payout_amount", "currency",
                           "new_trust_score", "trust_delta"],
        "frequency": "medium",
    },
    "task.rejected": {
        "description": "Fired when a task is graded as rejected.",
        "payload_fields": ["worker_id", "task_id", "task_type", "reason",
                           "new_trust_score", "trust_delta"],
        "frequency": "medium",
    },
    "worker.promoted": {
        "description": "Fired when a worker's tier changes (e.g. bronze → silver).",
        "payload_fields": ["worker_id", "from_tier", "to_tier", "trust_score"],
        "frequency": "low",
    },
    "worker.suspended": {
        "description": "Fired when a worker is suspended due to velocity or fraud.",
        "payload_fields": ["worker_id", "reason", "suspension_type",
                           "fraud_event_id", "duration_hours"],
        "frequency": "low",
    },
    "worker.score_update": {
        "description": "Fired when a worker's trust score changes by >= 5 points.",
        "payload_fields": ["worker_id", "trust_score_before", "trust_score_after",
                           "trust_delta", "algorithm_version", "lifecycle_stage"],
        "frequency": "medium",
    },
    "fraud.detected": {
        "description": "Fired when a fraud event is created (velocity, IP, coordinated).",
        "payload_fields": ["worker_id", "event_type", "severity", "reason_codes",
                           "auto_action", "progressive_level"],
        "frequency": "low",
    },
    "payout.sent": {
        "description": "Fired when a payout is confirmed and sent to a worker.",
        "payload_fields": ["worker_id", "amount", "currency", "ledger_entry_id"],
        "frequency": "low",
    },
    "payout.clawback": {
        "description": "Fired when earnings are clawed back after fraud review.",
        "payload_fields": ["worker_id", "amount", "currency", "reason",
                           "original_entry_id", "ledger_entry_id"],
        "frequency": "rare",
    },
    "appeal.submitted": {
        "description": "Fired when a worker submits an appeal.",
        "payload_fields": ["worker_id", "appeal_id", "fraud_event_id", "reason"],
        "frequency": "rare",
    },
    "appeal.resolved": {
        "description": "Fired when an admin resolves an appeal.",
        "payload_fields": ["worker_id", "appeal_id", "outcome", "reviewer_notes"],
        "frequency": "rare",
    },
}


# ─── Request / Response schemas ───────────────────────────────────────────────

class WebhookSubscription(BaseModel):
    url:    str      = Field(..., description="HTTPS endpoint to receive events")
    events: List[str] = Field(
        ...,
        description="Event names to subscribe to. Use ['*'] for all events.",
        example=["task.validated", "fraud.detected", "worker.promoted"],
    )
    secret: Optional[str] = Field(
        None,
        description="Signing secret. Used to compute X-Ghostless-Signature header. "
                    "Generate with: openssl rand -hex 32",
    )


class WebhookLogEntry(BaseModel):
    id:              str
    event:           str
    status_code:     Optional[int]
    success:         bool
    attempt:         int
    duration_ms:     Optional[int]
    sent_at:         str
    idempotency_key: Optional[str]


class WebhookDeadLetterEntry(BaseModel):
    id:             str
    event:          str
    total_attempts: int
    last_error:     Optional[str]
    created_at:     str
    is_replayed:    bool


class TestEventRequest(BaseModel):
    event:   str = Field(default="task.validated", description="Event type to send")
    payload: Optional[Dict[str, Any]] = Field(
        None,
        description="Custom payload. If omitted, a realistic example is generated.",
    )


# ─── Helper: expand wildcard subscriptions ────────────────────────────────────

def _expand_events(events: List[str]) -> List[str]:
    if "*" in events:
        return list(EVENT_CATALOG.keys())
    # Validate event names
    unknown = [e for e in events if e not in EVENT_CATALOG]
    if unknown:
        raise HTTPException(400, detail={
            "error": "unknown_events",
            "unknown": unknown,
            "valid_events": list(EVENT_CATALOG.keys()),
        })
    return events


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/events", summary="List all available webhook events")
async def list_events(auth: AuthContext = Depends(require_auth)):
    """
    Returns the full event catalog with descriptions, payload fields, and frequency.
    Use this to decide which events to subscribe to.
    """
    return {
        "events": {
            name: {
                "name":           name,
                "description":    info["description"],
                "payload_fields": info["payload_fields"],
                "frequency":      info["frequency"],
            }
            for name, info in EVENT_CATALOG.items()
        },
        "total": len(EVENT_CATALOG),
    }


@router.put("/subscription", summary="Configure webhook endpoint and event subscriptions")
async def configure_subscription(
    body: WebhookSubscription,
    auth: AuthContext  = Depends(require_auth),
    _:    None         = Depends(require_permission("tenants:update")),
    db:   AsyncSession = Depends(get_db),
):
    """
    Configure which events your endpoint receives and with what signing secret.

    Example (subscribe to everything):
        PUT /v1/webhooks/subscription
        {
            "url":    "https://yourapp.com/hooks/ghostless",
            "events": ["*"],
            "secret": "your-32-char-secret"
        }

    Example (targeted subscriptions):
        {
            "url":    "https://yourapp.com/hooks/ghostless",
            "events": ["fraud.detected", "worker.score_update", "payout.clawback"]
        }

    Signature verification (in your handler):
        signature = request.headers["X-Ghostless-Signature"]
        valid     = hmac.compare_digest(
            "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
            signature,
        )
    """
    expanded_events = _expand_events(body.events)

    await db.execute(
        update(Tenant)
        .where(Tenant.id == auth.tenant_id)
        .values(
            webhook_url    = str(body.url),
            webhook_events = expanded_events,
            webhook_secret = body.secret,
        )
    )
    await db.commit()

    return {
        "configured":   True,
        "url":          str(body.url),
        "events":       expanded_events,
        "event_count":  len(expanded_events),
        "signed":       body.secret is not None,
        "tip": (
            "Verify signatures using the X-Ghostless-Signature header. "
            "See docs at https://docs.ghostless.io/webhooks#verification"
        ),
    }


@router.post("/test", summary="Send a test event to your webhook endpoint")
async def send_test_event(
    body: TestEventRequest,
    auth: AuthContext  = Depends(require_auth),
    _:    None         = Depends(require_permission("tenants:update")),
    db:   AsyncSession = Depends(get_db),
):
    """
    Fire a test event to your configured webhook endpoint.
    Useful for verifying your handler is set up correctly.
    """
    if body.event not in EVENT_CATALOG:
        raise HTTPException(400, detail={
            "error": "unknown_event",
            "valid_events": list(EVENT_CATALOG.keys()),
        })

    # Build realistic example payload if not provided
    example_payloads = {
        "task.validated":   {"worker_id": "example_worker", "task_type": "survey",
                              "quality_score": 0.85, "allow_submit": True,
                              "flags": [], "anomaly_score": 0.12,
                              "decision": "accept", "risk_level": "low"},
        "worker.promoted":  {"worker_id": "example_worker", "from_tier": "bronze",
                              "to_tier": "silver", "trust_score": 52.4},
        "fraud.detected":   {"worker_id": "example_worker", "event_type": "velocity_breach",
                              "severity": "critical", "auto_action": "flag",
                              "progressive_level": 1},
        "worker.score_update": {"worker_id": "example_worker", "trust_score_before": 55.0,
                                 "trust_score_after": 62.3, "trust_delta": 7.3,
                                 "algorithm_version": "v6.0.0"},
    }

    payload = body.payload or example_payloads.get(body.event, {"test": True, "event": body.event})
    payload["_test"] = True  # mark as test event

    # Dispatch via Celery (same path as real events)
    import uuid
    task = dispatch_webhook.delay(
        tenant_id       = auth.tenant_id,
        event           = body.event,
        payload         = payload,
        idempotency_key = f"test_{uuid.uuid4().hex[:16]}",
    )

    return {
        "dispatched":    True,
        "event":         body.event,
        "celery_task_id": task.id,
        "payload":       payload,
        "note": "Check your endpoint for delivery. View logs at GET /v1/webhooks/logs",
    }


@router.get("/logs", response_model=List[WebhookLogEntry], summary="View webhook delivery history")
async def get_delivery_logs(
    limit:   int         = Query(50, le=200),
    event:   Optional[str] = Query(None, description="Filter by event type"),
    success: Optional[bool] = Query(None, description="Filter by delivery success"),
    auth:    AuthContext  = Depends(require_auth),
    _:       None         = Depends(require_permission("tenants:read")),
    db:      AsyncSession = Depends(get_db),
):
    """
    View recent webhook delivery attempts for your tenant.
    Shows status codes, delivery times, and retry counts.
    """
    query = (
        select(WebhookLog)
        .where(WebhookLog.tenant_id == auth.tenant_id)
        .order_by(desc(WebhookLog.sent_at))
        .limit(limit)
    )
    if event:
        query = query.where(WebhookLog.event == event)
    if success is not None:
        query = query.where(WebhookLog.success == success)

    result = await db.execute(query)
    logs   = result.scalars().all()

    return [
        WebhookLogEntry(
            id              = str(log.id),
            event           = log.event,
            status_code     = log.status_code,
            success         = log.success,
            attempt         = log.attempt,
            duration_ms     = log.duration_ms,
            sent_at         = log.sent_at.isoformat(),
            idempotency_key = log.idempotency_key,
        )
        for log in logs
    ]


@router.get("/dead-letters", response_model=List[WebhookDeadLetterEntry], summary="View failed (dead-lettered) webhook deliveries")
async def get_dead_letters(
    limit:  int         = Query(50, le=200),
    auth:   AuthContext = Depends(require_auth),
    _:      None        = Depends(require_permission("tenants:update")),
    db:     AsyncSession = Depends(get_db),
):
    """
    View webhook deliveries that failed all retry attempts and were moved to the dead-letter queue.
    Use POST /dead-letters/{id}/replay to retry them.
    """
    result = await db.execute(
        select(WebhookDeadLetter)
        .where(WebhookDeadLetter.tenant_id == auth.tenant_id)
        .where(WebhookDeadLetter.is_replayed == False)
        .order_by(desc(WebhookDeadLetter.created_at))
        .limit(limit)
    )
    items = result.scalars().all()

    return [
        WebhookDeadLetterEntry(
            id             = str(dl.id),
            event          = dl.event,
            total_attempts = dl.total_attempts,
            last_error     = dl.last_error,
            created_at     = dl.created_at.isoformat(),
            is_replayed    = dl.is_replayed,
        )
        for dl in items
    ]


@router.post("/dead-letters/{dead_letter_id}/replay", summary="Replay a failed webhook delivery")
async def replay_dead_letter(
    dead_letter_id: str,
    auth:   AuthContext = Depends(require_auth),
    _:      None        = Depends(require_permission("tenants:update")),
    db:     AsyncSession = Depends(get_db),
):
    """
    Retry a dead-lettered webhook delivery. Creates a new Celery task with a fresh
    idempotency key so the event is not treated as a duplicate.

    Typical use case:
      1. Your endpoint was down and missed some deliveries.
      2. Endpoint is back up.
      3. Call this endpoint for each dead letter to replay.
    """
    result = await db.execute(
        select(WebhookDeadLetter)
        .where(WebhookDeadLetter.id == dead_letter_id)
        .where(WebhookDeadLetter.tenant_id == auth.tenant_id)
    )
    dl = result.scalar_one_or_none()
    if not dl:
        raise HTTPException(404, detail={"error": "dead_letter_not_found"})
    if dl.is_replayed:
        raise HTTPException(409, detail={"error": "already_replayed", "replayed_at": str(dl.replayed_at)})

    import uuid
    from datetime import datetime, timezone
    new_key  = f"replay_{uuid.uuid4().hex[:16]}"
    task     = dispatch_webhook.delay(
        tenant_id       = auth.tenant_id,
        event           = dl.event,
        payload         = dl.payload,
        idempotency_key = new_key,
    )

    # Mark as replayed
    await db.execute(
        update(WebhookDeadLetter)
        .where(WebhookDeadLetter.id == dead_letter_id)
        .values(
            is_replayed = True,
            replayed_at = datetime.now(timezone.utc),
        )
    )
    await db.commit()

    return {
        "replayed":       True,
        "dead_letter_id": dead_letter_id,
        "event":          dl.event,
        "celery_task_id": task.id,
        "new_idempotency_key": new_key,
    }

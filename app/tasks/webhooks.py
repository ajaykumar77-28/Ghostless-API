"""
Ghostless API — Celery Tasks

webhooks.py  — dispatch signed webhook payloads to tenant endpoints
"""
import hashlib
import hmac
import json
import time
from typing import Optional

import httpx
from celery import Celery

from app.config import settings

celery_app = Celery(
    "ghostless",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

# ─── Webhook Dispatcher ───────────────────────────────────────────────────────

WEBHOOK_EVENTS = [
    "task.validated",
    "task.accepted",
    "task.rejected",
    "worker.promoted",
    "worker.suspended",
    "payout.sent",
    "bug.reported",
]


def _get_tenant_webhook_config(tenant_id: str) -> Optional[dict]:
    """Synchronous DB fetch for Celery context."""
    import psycopg2
    from app.config import settings

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    try:
        conn = psycopg2.connect(sync_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_url, webhook_secret, webhook_events FROM tenants WHERE id = %s AND is_active = TRUE",
            (tenant_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return {"url": row[0], "secret": row[1], "events": row[2] or []}
        return None
    except Exception as e:
        import structlog
        structlog.get_logger().error("webhook_config_fetch_failed", error=str(e))
        return None


@celery_app.task(
    bind=True,
    name="ghostless.dispatch_webhook",
    max_retries=settings.WEBHOOK_MAX_RETRIES,
    default_retry_delay=settings.WEBHOOK_RETRY_DELAY_SECONDS,
    autoretry_for=(httpx.HTTPError, httpx.TimeoutException),
)
def dispatch_webhook(self, tenant_id: str, event: str, payload: dict):
    """
    Dispatch a signed webhook to the tenant's registered endpoint.

    Signature:  X-Ghostless-Signature: sha256=<hmac_hex>
    Event:      X-Ghostless-Event: task.accepted
    Retry:      exponential backoff, up to 5 attempts
    Log:        every attempt written to webhook_logs table
    """
    config = _get_tenant_webhook_config(tenant_id)
    if not config or not config.get("url"):
        return {"skipped": True, "reason": "no_webhook_configured"}

    if event not in (config.get("events") or []):
        return {"skipped": True, "reason": "event_not_subscribed"}

    body = json.dumps({
        "event":      event,
        "tenant_id":  tenant_id,
        "data":       payload,
        "timestamp":  int(time.time()),
        "attempt":    self.request.retries + 1,
    }, sort_keys=True)

    secret = config["secret"] or ""
    signature = "sha256=" + hmac.new(
        secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    start = time.time()
    try:
        resp = httpx.post(
            config["url"],
            content=body,
            headers={
                "Content-Type":           "application/json",
                "X-Ghostless-Signature":  signature,
                "X-Ghostless-Event":      event,
                "X-Ghostless-Attempt":    str(self.request.retries + 1),
                "User-Agent":             "Ghostless-Webhook/1.0",
            },
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
        )
        duration_ms = int((time.time() - start) * 1000)
        success = resp.status_code < 400
        _log_webhook_attempt(tenant_id, event, payload, resp.status_code, success, duration_ms, self.request.retries + 1)
        resp.raise_for_status()
        return {"delivered": True, "status_code": resp.status_code, "duration_ms": duration_ms}

    except httpx.HTTPStatusError as e:
        _log_webhook_attempt(tenant_id, event, payload, e.response.status_code, False, 0, self.request.retries + 1)
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


def _log_webhook_attempt(tenant_id, event, payload, status_code, success, duration_ms, attempt):
    """Write webhook attempt to DB (fire and forget via separate connection)."""
    try:
        import psycopg2
        from app.config import settings
        sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
        conn = psycopg2.connect(sync_url)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO webhook_logs (id, tenant_id, event, payload, status_code, attempt, success, duration_ms)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s)""",
            (tenant_id, event, json.dumps(payload), status_code, attempt, success, duration_ms),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-fatal — don't let logging break delivery

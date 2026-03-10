"""
Ghostless API — Webhook Dispatcher (v2)

Upgrades from v1:
  - Exponential backoff with jitter
  - Idempotency keys (prevents double delivery on retry)
  - Dead-letter queue table (WebhookDeadLetter) after max retries
  - Delivery status tracking per attempt
  - Redis deduplication window
"""
import hashlib
import hmac
import json
import time
import uuid
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

WEBHOOK_EVENTS = [
    "task.validated",
    "task.accepted",
    "task.rejected",
    "worker.promoted",
    "worker.suspended",
    "payout.sent",
    "bug.reported",
]


def _get_sync_conn():
    import psycopg2
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    return psycopg2.connect(sync_url)


def _get_tenant_webhook_config(tenant_id: str) -> Optional[dict]:
    try:
        conn = _get_sync_conn()
        cur  = conn.cursor()
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


def _is_duplicate(idempotency_key: str, redis_url: str) -> bool:
    """Check Redis dedup window to prevent double delivery."""
    import redis as sync_redis
    try:
        r   = sync_redis.from_url(redis_url)
        key = f"webhook_sent:{idempotency_key}"
        result = r.set(key, "1", nx=True, ex=settings.DEDUP_WINDOW_SECONDS)
        return result is None   # None = key already existed = duplicate
    except Exception:
        return False   # on Redis error, allow through


def _log_webhook_attempt(tenant_id, event, payload, status_code, success, duration_ms, attempt, idempotency_key):
    try:
        conn = _get_sync_conn()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO webhook_logs
               (id, tenant_id, event, payload, status_code, attempt, success, duration_ms, idempotency_key)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s)""",
            (tenant_id, event, json.dumps(payload), status_code, attempt, success, duration_ms, idempotency_key),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _send_to_dead_letter(tenant_id, event, payload, total_attempts, last_error, idempotency_key):
    """Write to dead_letter table after all retries exhausted."""
    try:
        conn = _get_sync_conn()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO webhook_dead_letters
               (id, tenant_id, event, payload, total_attempts, last_error, idempotency_key)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (tenant_id, event, json.dumps(payload), total_attempts, str(last_error), idempotency_key),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


@celery_app.task(
    bind=True,
    name="ghostless.dispatch_webhook",
    max_retries=settings.WEBHOOK_MAX_RETRIES,
    # No default_retry_delay — we set countdown manually for exponential backoff
)
def dispatch_webhook(
    self,
    tenant_id:       str,
    event:           str,
    payload:         dict,
    idempotency_key: Optional[str] = None,
):
    """
    Dispatch a signed webhook with:
      - Idempotency key dedup (Redis)
      - Exponential backoff: 60s, 120s, 240s, 480s, 960s
      - Dead-letter queue after max retries
      - Attempt tracking in webhook_logs
    """
    # Generate idempotency key if not provided
    if not idempotency_key:
        idempotency_key = hashlib.sha256(
            f"{tenant_id}:{event}:{json.dumps(payload, sort_keys=True)}".encode()
        ).hexdigest()[:32]

    # Dedup check (only on first attempt)
    if self.request.retries == 0 and _is_duplicate(idempotency_key, settings.CELERY_BROKER_URL):
        return {"skipped": True, "reason": "duplicate_delivery"}

    config = _get_tenant_webhook_config(tenant_id)
    if not config or not config.get("url"):
        return {"skipped": True, "reason": "no_webhook_configured"}

    if event not in (config.get("events") or []):
        return {"skipped": True, "reason": "event_not_subscribed"}

    body = json.dumps({
        "event":           event,
        "tenant_id":       tenant_id,
        "data":            payload,
        "timestamp":       int(time.time()),
        "attempt":         self.request.retries + 1,
        "idempotency_key": idempotency_key,
    }, sort_keys=True)

    secret    = config.get("secret") or ""
    signature = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    attempt   = self.request.retries + 1
    start     = time.time()

    try:
        resp = httpx.post(
            config["url"],
            content=body,
            headers={
                "Content-Type":           "application/json",
                "X-Ghostless-Signature":  signature,
                "X-Ghostless-Event":      event,
                "X-Ghostless-Attempt":    str(attempt),
                "X-Ghostless-Idempotency": idempotency_key,
                "User-Agent":             "Ghostless-Webhook/2.0",
            },
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
        )
        duration_ms = int((time.time() - start) * 1000)
        success = resp.status_code < 400
        _log_webhook_attempt(
            tenant_id, event, payload, resp.status_code, success,
            duration_ms, attempt, idempotency_key,
        )

        if not success:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )

        return {"delivered": True, "status_code": resp.status_code, "duration_ms": duration_ms}

    except (httpx.HTTPError, httpx.TimeoutException) as e:
        duration_ms = int((time.time() - start) * 1000)
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        _log_webhook_attempt(
            tenant_id, event, payload, status_code, False,
            duration_ms, attempt, idempotency_key,
        )

        if self.request.retries >= settings.WEBHOOK_MAX_RETRIES:
            # All retries exhausted → dead-letter queue
            _send_to_dead_letter(
                tenant_id, event, payload,
                total_attempts=attempt,
                last_error=str(e),
                idempotency_key=idempotency_key,
            )
            return {"dead_lettered": True, "error": str(e)}

        # Exponential backoff with jitter: 60 * 2^retry ± 10%
        import random
        base_delay = 60 * (2 ** self.request.retries)
        jitter     = random.uniform(0.9, 1.1)
        countdown  = int(base_delay * jitter)
        raise self.retry(exc=e, countdown=countdown)

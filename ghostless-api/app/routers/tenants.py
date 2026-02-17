"""
Ghostless API — Tenant Management Router

POST /v1/tenants                      — create a new tenant (your sales flow)
GET  /v1/tenants/me                   — get current tenant info
POST /v1/tenants/me/api-keys          — generate a new API key
DELETE /v1/tenants/me/api-keys/{id}   — revoke an API key
PUT  /v1/tenants/me/webhooks          — configure webhook endpoint
GET  /v1/tenants/me/usage             — current month usage stats
"""
import hashlib
import secrets
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, require_auth, hash_api_key
from app.models.models import APIKey, Tenant, Validation, Worker

router = APIRouter(prefix="/tenants", tags=["Tenants"])


# ─── Schemas ─────────────────────────────────────────────────────────────────

class CreateTenantRequest(BaseModel):
    name:        str
    slug:        str         # unique URL-safe identifier: "acme-corp"
    tier:        str = "starter"
    brand_name:  Optional[str] = None
    brand_color: Optional[str] = None

    @validator("tier")
    def validate_tier(cls, v):
        if v not in ("starter", "growth", "enterprise"):
            raise ValueError("tier must be: starter | growth | enterprise")
        return v

    @validator("slug")
    def validate_slug(cls, v):
        import re
        if not re.match(r"^[a-z0-9-]{3,50}$", v):
            raise ValueError("slug must be lowercase alphanumeric with hyphens, 3–50 chars")
        return v


class CreateAPIKeyRequest(BaseModel):
    name: str   # "production-key", "staging-key"
    expires_in_days: Optional[int] = None


class UpdateWebhookRequest(BaseModel):
    webhook_url:    str
    webhook_secret: Optional[str] = None   # auto-generated if not provided
    events:         List[str]

    @validator("events", each_item=True)
    def validate_events(cls, v):
        valid = [
            "task.validated", "task.accepted", "task.rejected",
            "worker.promoted", "worker.suspended",
            "payout.sent", "bug.reported",
        ]
        if v not in valid:
            raise ValueError(f"Invalid event '{v}'. Valid events: {valid}")
        return v


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("", summary="Create a new tenant (onboard a client platform)")
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    # Note: This endpoint would normally be protected by a master admin key
    # For simplicity, we show it open here. In production, gate it.
):
    """
    Onboard a new client platform. Returns their first API key.
    Run this from your sales/onboarding flow when a new client signs up.
    """
    # Check slug uniqueness
    existing = await db.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail={"error": "slug_taken", "slug": body.slug})

    # Generate first API key
    raw_key = settings.API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:20] + "••••••••"

    tenant = Tenant(
        name=body.name,
        slug=body.slug,
        tier=body.tier,
        brand_name=body.brand_name,
        brand_color=body.brand_color,
    )
    db.add(tenant)
    await db.flush()

    api_key = APIKey(
        tenant_id=tenant.id,
        name="default",
        key_hash=key_hash,
        key_prefix=key_prefix,
    )
    db.add(api_key)
    await db.commit()

    return {
        "tenant_id":  str(tenant.id),
        "slug":       tenant.slug,
        "tier":       tenant.tier,
        "api_key":    raw_key,    # ⚠ Only shown once — client must store this securely
        "key_id":     str(api_key.id),
        "message":    "Store your API key securely — it will not be shown again.",
        "docs_url":   "https://docs.ghostless.io",
    }


@router.get("/me", summary="Get current tenant info")
async def get_tenant_info(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Tenant).where(Tenant.id == auth.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, detail={"error": "tenant_not_found"})

    # Count workers
    worker_count = await db.execute(
        select(func.count(Worker.id)).where(Worker.tenant_id == auth.tenant_id)
    )

    return {
        "id":            str(tenant.id),
        "name":          tenant.name,
        "slug":          tenant.slug,
        "tier":          tenant.tier.value,
        "brand_name":    tenant.brand_name,
        "brand_color":   tenant.brand_color,
        "webhook_url":   tenant.webhook_url,
        "webhook_events": tenant.webhook_events or [],
        "is_active":     tenant.is_active,
        "created_at":    tenant.created_at.isoformat(),
        "worker_count":  worker_count.scalar(),
    }


@router.post("/me/api-keys", summary="Generate a new API key")
async def create_api_key(
    body: CreateAPIKeyRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new API key for this tenant. Use for key rotation."""
    raw_key = settings.API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)

    expires_at = None
    if body.expires_in_days:
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(days=body.expires_in_days)

    api_key = APIKey(
        tenant_id=auth.tenant_id,
        name=body.name,
        key_hash=key_hash,
        key_prefix=raw_key[:20] + "••••••••",
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.commit()

    return {
        "key_id":   str(api_key.id),
        "name":     body.name,
        "api_key":  raw_key,   # ⚠ Only shown once
        "expires_at": expires_at.isoformat() if expires_at else None,
        "message":  "Store your API key securely — it will not be shown again.",
    }


@router.delete("/me/api-keys/{key_id}", summary="Revoke an API key")
async def revoke_api_key(
    key_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(400, detail={"error": "invalid_key_id"})

    result = await db.execute(
        select(APIKey)
        .where(APIKey.id == key_uuid)
        .where(APIKey.tenant_id == auth.tenant_id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(404, detail={"error": "key_not_found"})

    from sqlalchemy import update as sql_update
    await db.execute(sql_update(APIKey).where(APIKey.id == key_uuid).values(is_active=False))
    await db.commit()

    # Invalidate Redis cache for this tenant
    from app.middleware.auth import get_redis
    redis = await get_redis()
    await redis.delete(f"tenant_config:{auth.tenant_id}")

    return {"revoked": True, "key_id": key_id}


@router.put("/me/webhooks", summary="Configure webhook endpoint")
async def update_webhooks(
    body: UpdateWebhookRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Set the webhook URL and subscribe to events.
    A secret is auto-generated if not provided — use it to verify incoming payloads
    via the X-Ghostless-Signature header (HMAC-SHA256).
    """
    secret = body.webhook_secret or secrets.token_urlsafe(32)

    from sqlalchemy import update as sql_update
    await db.execute(
        sql_update(Tenant)
        .where(Tenant.id == auth.tenant_id)
        .values(
            webhook_url=body.webhook_url,
            webhook_secret=secret,
            webhook_events=body.events,
        )
    )
    await db.commit()

    # Invalidate tenant config cache
    from app.middleware.auth import get_redis
    redis = await get_redis()
    await redis.delete(f"tenant_config:{auth.tenant_id}")

    return {
        "webhook_url":    body.webhook_url,
        "webhook_secret": secret,  # ⚠ Store this — used to verify signatures
        "subscribed_events": body.events,
        "signature_header": "X-Ghostless-Signature",
        "signature_format": "sha256=<hmac_hex>",
    }


@router.get("/me/usage", summary="Current period usage stats")
async def get_usage(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Returns API usage for billing/monitoring."""
    from datetime import timedelta
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    validations = await db.execute(
        select(func.count(Validation.id))
        .where(Validation.tenant_id == auth.tenant_id)
        .where(Validation.validated_at >= month_start)
    )
    workers = await db.execute(
        select(func.count(Worker.id)).where(Worker.tenant_id == auth.tenant_id)
    )

    val_count = validations.scalar() or 0
    worker_count = workers.scalar() or 0

    tier_limits = {
        "starter":    50_000,
        "growth":     500_000,
        "enterprise": None,  # unlimited
    }
    limit = tier_limits.get(auth.tenant_tier)

    return {
        "period": month_start.strftime("%Y-%m"),
        "validations_this_month": val_count,
        "validations_limit": limit,
        "utilization_pct": round(val_count / limit * 100, 1) if limit else None,
        "active_workers": worker_count,
        "tier": auth.tenant_tier,
        "billing_period_ends": (month_start + timedelta(days=31)).strftime("%Y-%m-01"),
    }

"""
Ghostless API — Authentication & Authorization Middleware

Two auth modes:
  1. Server-to-server:  X-API-Key header  (hashed HMAC, stored in DB)
  2. Worker-facing:     Bearer JWT token  (short-lived, 1 hour)

Every request also requires X-Tenant-ID.
Rate limiting enforced per tenant per minute via Redis sliding window.
"""
import hashlib
import hmac
import time
from datetime import datetime, timedelta
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.models import APIKey, Tenant

# ─── Redis singleton ──────────────────────────────────────────────────────────
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


# ─── Token helpers ────────────────────────────────────────────────────────────

def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_worker_token(worker_id: str, tenant_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": worker_id, "tenant_id": tenant_id, "exp": expire, "type": "worker"},
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_tenant_token(tenant_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": tenant_id, "tenant_id": tenant_id, "exp": expire, "type": "tenant"},
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


# ─── Rate limiter (sliding window per tenant per minute) ─────────────────────

TIER_LIMITS = {
    "starter":    settings.RATE_LIMIT_STARTER,
    "growth":     settings.RATE_LIMIT_GROWTH,
    "enterprise": settings.RATE_LIMIT_ENTERPRISE,
}


async def enforce_rate_limit(tenant_id: str, tenant_tier: str, redis: aioredis.Redis):
    limit = TIER_LIMITS.get(tenant_tier, settings.RATE_LIMIT_STARTER)
    window = int(time.time() // 60)
    key = f"ratelimit:{tenant_id}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 120)  # 2-minute TTL for cleanup
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "message": f"Limit: {limit} req/min for {tenant_tier} tier.",
                "limit": limit,
                "reset_in_seconds": 60 - (int(time.time()) % 60),
            },
        )


# ─── Dependency: resolve tenant from DB or cache ─────────────────────────────

async def _get_tenant_from_cache(tenant_id: str, redis: aioredis.Redis) -> Optional[dict]:
    import json
    cached = await redis.get(f"tenant_config:{tenant_id}")
    if cached:
        return json.loads(cached)
    return None


async def _cache_tenant(tenant: dict, redis: aioredis.Redis):
    import json
    await redis.setex(
        f"tenant_config:{tenant['id']}",
        settings.CACHE_TTL_TENANT_CONFIG,
        json.dumps(tenant),
    )


# ─── Main Auth Dependency ─────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


class AuthContext:
    """Injected into every authenticated route."""
    def __init__(
        self,
        tenant_id: str,
        tenant_tier: str,
        auth_type: str,
        worker_id: Optional[str] = None,
    ):
        self.tenant_id   = tenant_id
        self.tenant_tier = tenant_tier
        self.auth_type   = auth_type     # "api_key" | "jwt"
        self.worker_id   = worker_id


async def require_auth(
    request: Request,
    x_tenant_id: str = Header(..., description="Your tenant slug, e.g. acme-corp"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    redis = await get_redis()

    # ── Mode 1: API Key ───────────────────────────────────────────────────
    if x_api_key:
        key_hash = hash_api_key(x_api_key)
        result = await db.execute(
            select(APIKey, Tenant)
            .join(Tenant, APIKey.tenant_id == Tenant.id)
            .where(APIKey.key_hash == key_hash)
            .where(APIKey.is_active == True)
            .where(Tenant.slug == x_tenant_id)
            .where(Tenant.is_active == True)
        )
        row = result.first()
        if not row:
            raise HTTPException(401, detail={"error": "invalid_api_key", "message": "API key not found or inactive."})

        api_key, tenant = row
        tenant_tier = tenant.tier.value

        # Cache it
        await _cache_tenant({"id": str(tenant.id), "tier": tenant_tier}, redis)

        # Rate limit
        await enforce_rate_limit(str(tenant.id), tenant_tier, redis)

        # Update last_used_at asynchronously (fire and forget in production)
        from sqlalchemy import update as sql_update
        await db.execute(
            sql_update(APIKey)
            .where(APIKey.id == api_key.id)
            .values(last_used_at=datetime.utcnow())
        )

        return AuthContext(
            tenant_id=str(tenant.id),
            tenant_tier=tenant_tier,
            auth_type="api_key",
        )

    # ── Mode 2: Bearer JWT ────────────────────────────────────────────────
    if credentials and credentials.scheme.lower() == "bearer":
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
        except JWTError as e:
            raise HTTPException(401, detail={"error": "invalid_token", "message": str(e)})

        # Validate tenant match
        if payload.get("tenant_id") != x_tenant_id:
            raise HTTPException(403, detail={"error": "tenant_mismatch"})

        # Get tenant tier (from cache or DB)
        tenant_data = await _get_tenant_from_cache(payload["tenant_id"], redis)
        if not tenant_data:
            result = await db.execute(
                select(Tenant).where(Tenant.slug == x_tenant_id).where(Tenant.is_active == True)
            )
            tenant = result.scalar_one_or_none()
            if not tenant:
                raise HTTPException(404, detail={"error": "tenant_not_found"})
            tenant_data = {"id": str(tenant.id), "tier": tenant.tier.value}
            await _cache_tenant(tenant_data, redis)

        await enforce_rate_limit(tenant_data["id"], tenant_data["tier"], redis)

        return AuthContext(
            tenant_id=tenant_data["id"],
            tenant_tier=tenant_data["tier"],
            auth_type="jwt",
            worker_id=payload.get("sub"),
        )

    raise HTTPException(
        401,
        detail={
            "error": "missing_credentials",
            "message": "Provide X-API-Key header or Bearer token.",
        },
    )


# ─── Webhook signature verification (for inbound webhook validation) ─────────

def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

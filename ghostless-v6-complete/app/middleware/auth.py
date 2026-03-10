"""
Ghostless API — Authentication & Authorization Middleware (v2)

Upgrades from v1:
  - argon2 API key verification (falls back to sha256 for legacy keys)
  - JWT with iss/aud/jti claims + expiry enforcement
  - Per-tenant JWT signing secret
  - JTI revocation blacklist via Redis
  - Worker generation-based mass revocation
  - Refresh token endpoint support
  - Tenant isolation enforced at middleware layer
  - Rate limiting per API key (not just per tenant)
"""
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.models import APIKey, Tenant
from app.services.auth import (
    decode_token,
    is_token_revoked,
    is_worker_generation_valid,
    verify_api_key,
)

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


# ─── Rate limiter ─────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "starter":    settings.RATE_LIMIT_STARTER,
    "growth":     settings.RATE_LIMIT_GROWTH,
    "enterprise": settings.RATE_LIMIT_ENTERPRISE,
}


async def enforce_rate_limit(
    tenant_id:   str,
    tier:        str,
    redis:       aioredis.Redis,
    api_key_id:  Optional[str] = None,
    key_rpm_override: Optional[int] = None,
) -> None:
    """
    Sliding window per tenant per minute.
    If api_key_id provided, ALSO enforce per-key rate limit.
    """
    limit = key_rpm_override or TIER_LIMITS.get(tier, settings.RATE_LIMIT_STARTER)
    window = int(time.time() // 60)

    # Tenant-level check
    tenant_key = f"ratelimit:tenant:{tenant_id}:{window}"
    count = await redis.incr(tenant_key)
    if count == 1:
        await redis.expire(tenant_key, 120)
    if count > limit:
        raise HTTPException(
            429,
            detail={
                "error":            "rate_limit_exceeded",
                "message":          f"Limit: {limit} req/min.",
                "reset_in_seconds": 60 - (int(time.time()) % 60),
            },
        )

    # Per-key check (if key has its own limit)
    if api_key_id and key_rpm_override:
        key_rl_key = f"ratelimit:key:{api_key_id}:{window}"
        key_count  = await redis.incr(key_rl_key)
        if key_count == 1:
            await redis.expire(key_rl_key, 120)
        if key_count > key_rpm_override:
            raise HTTPException(
                429,
                detail={
                    "error":   "key_rate_limit_exceeded",
                    "message": f"This API key limit: {key_rpm_override} req/min.",
                },
            )


# ─── Tenant cache ─────────────────────────────────────────────────────────────

async def _get_tenant_cached(tenant_id: str, redis: aioredis.Redis) -> Optional[dict]:
    import json
    cached = await redis.get(f"tenant_config:{tenant_id}")
    return json.loads(cached) if cached else None


async def _cache_tenant(tenant_data: dict, redis: aioredis.Redis) -> None:
    import json
    await redis.setex(
        f"tenant_config:{tenant_data['id']}",
        settings.CACHE_TTL_TENANT_CONFIG,
        json.dumps(tenant_data),
    )


# ─── AuthContext ──────────────────────────────────────────────────────────────

class AuthContext:
    def __init__(
        self,
        tenant_id:         str,
        tenant_tier:       str,
        auth_type:         str,
        worker_id:         Optional[str] = None,
        jwt_jti:           Optional[str] = None,
        api_key_id:        Optional[str] = None,
        tenant_jwt_secret: Optional[str] = None,
    ):
        self.tenant_id         = tenant_id
        self.tenant_tier       = tenant_tier
        self.auth_type         = auth_type   # "api_key" | "jwt"
        self.worker_id         = worker_id
        self.jwt_jti           = jwt_jti
        self.api_key_id        = api_key_id
        self.tenant_jwt_secret = tenant_jwt_secret


security = HTTPBearer(auto_error=False)


async def require_auth(
    request:     Request,
    x_tenant_id: str = Header(..., description="Tenant slug, e.g. acme-corp"),
    x_api_key:   Optional[str] = Header(None, alias="X-API-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db:          AsyncSession = Depends(get_db),
) -> AuthContext:
    redis = await get_redis()

    # ── Mode 1: API Key ────────────────────────────────────────────────────
    if x_api_key:
        # Fetch candidate keys for this tenant by prefix (fast lookup)
        key_prefix = x_api_key[:12]
        result = await db.execute(
            select(APIKey, Tenant)
            .join(Tenant, APIKey.tenant_id == Tenant.id)
            .where(APIKey.key_prefix == key_prefix)
            .where(APIKey.is_active == True)
            .where(APIKey.revoked_at == None)
            .where(Tenant.slug == x_tenant_id)
            .where(Tenant.is_active == True)
        )
        rows = result.all()

        matched_row = None
        for api_key, tenant in rows:
            if verify_api_key(x_api_key, api_key.key_hash, api_key.hash_algorithm or "argon2"):
                matched_row = (api_key, tenant)
                break

        if not matched_row:
            raise HTTPException(
                401,
                detail={"error": "invalid_api_key", "message": "API key not found or inactive."},
            )

        api_key, tenant = matched_row

        # Check expiry
        if api_key.expires_at and api_key.expires_at < datetime.now(tz=timezone.utc):
            raise HTTPException(401, detail={"error": "api_key_expired"})

        tenant_data = {"id": str(tenant.id), "tier": tenant.tier.value, "jwt_secret": tenant.jwt_secret}
        await _cache_tenant(tenant_data, redis)

        await enforce_rate_limit(
            str(tenant.id),
            tenant.tier.value,
            redis,
            api_key_id=str(api_key.id),
            key_rpm_override=api_key.rate_limit_rpm,
        )

        # Fire-and-forget last_used_at update
        await db.execute(
            sql_update(APIKey)
            .where(APIKey.id == api_key.id)
            .values(last_used_at=datetime.now(tz=timezone.utc))
        )

        return AuthContext(
            tenant_id         = str(tenant.id),
            tenant_tier       = tenant.tier.value,
            auth_type         = "api_key",
            api_key_id        = str(api_key.id),
            tenant_jwt_secret = tenant.jwt_secret,
        )

    # ── Mode 2: Bearer JWT ─────────────────────────────────────────────────
    if credentials and credentials.scheme.lower() == "bearer":
        # Need tenant's jwt_secret to decode — fetch tenant first
        tenant_data = await _get_tenant_cached(x_tenant_id, redis)
        if not tenant_data:
            result = await db.execute(
                select(Tenant).where(Tenant.slug == x_tenant_id).where(Tenant.is_active == True)
            )
            tenant = result.scalar_one_or_none()
            if not tenant:
                raise HTTPException(404, detail={"error": "tenant_not_found"})
            tenant_data = {"id": str(tenant.id), "tier": tenant.tier.value, "jwt_secret": tenant.jwt_secret}
            await _cache_tenant(tenant_data, redis)

        try:
            payload = decode_token(
                credentials.credentials,
                tenant_data["id"],
                tenant_jwt_secret=tenant_data.get("jwt_secret"),
            )
        except JWTError as e:
            raise HTTPException(401, detail={"error": "invalid_token", "message": str(e)})

        # Verify tenant matches
        if payload.get("tenant_id") != tenant_data["id"]:
            raise HTTPException(403, detail={"error": "tenant_mismatch"})

        # Check JTI blacklist (revoked individual tokens)
        jti = payload.get("jti")
        if jti and await is_token_revoked(jti, redis):
            raise HTTPException(401, detail={"error": "token_revoked"})

        # Check worker generation (mass revocation)
        worker_id = payload.get("sub") if payload.get("type") == "worker" else None
        if worker_id:
            token_iat = payload.get("iat", 0)
            valid = await is_worker_generation_valid(tenant_data["id"], worker_id, token_iat, redis)
            if not valid:
                raise HTTPException(401, detail={"error": "token_revoked", "reason": "worker_tokens_reset"})

        await enforce_rate_limit(tenant_data["id"], tenant_data["tier"], redis)

        return AuthContext(
            tenant_id         = tenant_data["id"],
            tenant_tier       = tenant_data["tier"],
            auth_type         = "jwt",
            worker_id         = worker_id,
            jwt_jti           = jti,
            tenant_jwt_secret = tenant_data.get("jwt_secret"),
        )

    raise HTTPException(
        401,
        detail={
            "error":   "missing_credentials",
            "message": "Provide X-API-Key header or Bearer token.",
        },
    )


# ─── Webhook signature verification ──────────────────────────────────────────

import hmac as _hmac


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + _hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, signature)

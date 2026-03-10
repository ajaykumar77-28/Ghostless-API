"""
Ghostless API — Auth Service (v2)

Upgrades from v1:
  - argon2 hashing for API keys (bcrypt fallback supported)
  - Refresh token issuance, rotation, and revocation
  - JWT audience + issuer claims
  - Per-tenant JWT signing secret (falls back to global SECRET_KEY)
  - Redis-backed access-token revocation blacklist (jti)
  - Key rotation helpers
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import uuid

from jose import JWTError, jwt
from passlib.hash import argon2 as argon2_hasher

from app.config import settings


# ─── Constants ────────────────────────────────────────────────────────────────

JWT_ISSUER        = "ghostless-api"
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


# ─── API Key hashing (argon2) ─────────────────────────────────────────────────

def generate_api_key() -> Tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (raw_key, key_hash, key_prefix)
    raw_key is shown once to the user and never stored.
    key_hash is stored in DB.
    key_prefix is first 12 chars for UI display.
    """
    raw = settings.API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash   = hash_api_key_argon2(raw)
    key_prefix = raw[:12]
    return raw, key_hash, key_prefix


def hash_api_key_argon2(raw_key: str) -> str:
    """Hash using argon2id. Suitable for storage."""
    return argon2_hasher.hash(raw_key)


def verify_api_key_argon2(raw_key: str, stored_hash: str) -> bool:
    """Verify a raw key against an argon2 hash."""
    try:
        return argon2_hasher.verify(stored_hash, raw_key)
    except Exception:
        return False


def hash_api_key_sha256(raw_key: str) -> str:
    """Legacy SHA-256 hash for backward compat."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str, algorithm: str = "argon2") -> bool:
    """Dispatch to correct verifier based on stored algorithm."""
    if algorithm == "argon2":
        return verify_api_key_argon2(raw_key, stored_hash)
    elif algorithm == "sha256":
        return stored_hash == hash_api_key_sha256(raw_key)
    return False


# ─── Refresh token helpers ────────────────────────────────────────────────────

def generate_refresh_token() -> Tuple[str, str]:
    """
    Generate a secure refresh token.
    Returns (raw_token, token_hash).
    raw_token is sent to client; only hash is stored.
    """
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


# ─── JWT helpers ──────────────────────────────────────────────────────────────

def _signing_secret(tenant_jwt_secret: Optional[str] = None) -> str:
    """Use per-tenant secret if available, fall back to global."""
    return tenant_jwt_secret or settings.SECRET_KEY


def create_access_token(
    subject: str,
    tenant_id: str,
    token_type: str = "worker",
    tenant_jwt_secret: Optional[str] = None,
    additional_claims: dict = None,
) -> str:
    """
    Issue a short-lived access JWT with full RFC 7519 claims:
      iss, aud, sub, iat, exp, jti, tenant_id, type
    """
    now    = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jti    = str(uuid.uuid4())

    claims = {
        "iss":       JWT_ISSUER,
        "aud":       f"ghostless:{tenant_id}",
        "sub":       subject,
        "iat":       now,
        "exp":       expire,
        "jti":       jti,
        "tenant_id": tenant_id,
        "type":      token_type,
    }
    if additional_claims:
        claims.update(additional_claims)

    return jwt.encode(claims, _signing_secret(tenant_jwt_secret), algorithm=settings.JWT_ALGORITHM)


def create_refresh_token_jwt(
    subject: str,
    tenant_id: str,
    family_id: str,
    tenant_jwt_secret: Optional[str] = None,
) -> str:
    """Issue a long-lived refresh JWT. The raw token is ALSO stored hashed in DB."""
    now    = datetime.now(tz=timezone.utc)
    expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    jti    = str(uuid.uuid4())

    claims = {
        "iss":       JWT_ISSUER,
        "aud":       f"ghostless:{tenant_id}",
        "sub":       subject,
        "iat":       now,
        "exp":       expire,
        "jti":       jti,
        "tenant_id": tenant_id,
        "type":      REFRESH_TOKEN_TYPE,
        "family_id": family_id,
    }
    return jwt.encode(claims, _signing_secret(tenant_jwt_secret), algorithm=settings.JWT_ALGORITHM)


def decode_token(
    token: str,
    tenant_id: str,
    tenant_jwt_secret: Optional[str] = None,
) -> dict:
    """
    Decode and validate a JWT. Raises JWTError on any failure.
    Validates iss, aud, exp automatically.
    """
    payload = jwt.decode(
        token,
        _signing_secret(tenant_jwt_secret),
        algorithms=[settings.JWT_ALGORITHM],
        audience=f"ghostless:{tenant_id}",
        issuer=JWT_ISSUER,
        options={"verify_exp": True},
    )
    return payload


# ─── Token revocation (Redis blacklist for access tokens) ─────────────────────

BLACKLIST_PREFIX = "jwt_blacklist:"


async def revoke_access_token(jti: str, expires_in_seconds: int, redis) -> None:
    """Add a JTI to the Redis blacklist. TTL matches token expiry."""
    await redis.setex(f"{BLACKLIST_PREFIX}{jti}", expires_in_seconds, "1")


async def is_token_revoked(jti: str, redis) -> bool:
    """Check if a JTI has been blacklisted."""
    result = await redis.exists(f"{BLACKLIST_PREFIX}{jti}")
    return bool(result)


async def revoke_all_worker_tokens(tenant_id: str, worker_id: str, redis) -> None:
    """
    Revoke all access tokens for a worker by setting a generation marker.
    Access tokens issued before this timestamp are considered invalid.
    Complement: store 'issued_at' in JWT and compare.
    """
    key = f"worker_revoke_gen:{tenant_id}:{worker_id}"
    await redis.set(key, datetime.now(tz=timezone.utc).timestamp())
    # TTL = longest possible token lifetime
    await redis.expire(key, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 60)


async def is_worker_generation_valid(
    tenant_id: str,
    worker_id: str,
    token_iat: float,
    redis,
) -> bool:
    """Return True if token was issued AFTER the revocation generation marker."""
    key = f"worker_revoke_gen:{tenant_id}:{worker_id}"
    marker = await redis.get(key)
    if marker is None:
        return True
    return token_iat >= float(marker)

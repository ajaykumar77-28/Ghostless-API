"""
Unit tests for auth service (services/auth.py).

Tests cover:
  - argon2 key hashing and verification
  - sha256 legacy verification
  - JWT creation with iss/aud/jti claims
  - JWT decode validation
  - JWT audience enforcement
  - Refresh token generation
  - EWMA alpha and confidence weight (via scoring import)
"""
import time
import pytest
from datetime import datetime, timezone, timedelta
from jose import jwt as jose_jwt, JWTError

from app.services.auth import (
    generate_api_key,
    hash_api_key_sha256,
    verify_api_key,
    verify_api_key_argon2,
    hash_api_key_argon2,
    generate_refresh_token,
    create_access_token,
    decode_token,
    JWT_ISSUER,
)
from app.config import settings


# ─── API Key hashing ──────────────────────────────────────────────────────────

class TestAPIKeyHashing:
    def test_generate_returns_three_parts(self):
        raw, hash_, prefix = generate_api_key()
        assert raw.startswith(settings.API_KEY_PREFIX)
        assert len(hash_) > 20
        assert prefix == raw[:12]

    def test_argon2_roundtrip(self):
        raw = "sk_live_gl_testkey123abc"
        h   = hash_api_key_argon2(raw)
        assert verify_api_key_argon2(raw, h)

    def test_argon2_wrong_key_fails(self):
        h = hash_api_key_argon2("correct-key")
        assert not verify_api_key_argon2("wrong-key", h)

    def test_sha256_roundtrip(self):
        raw  = "sk_live_gl_legacykey"
        h    = hash_api_key_sha256(raw)
        assert verify_api_key(raw, h, algorithm="sha256")

    def test_sha256_wrong_key_fails(self):
        h = hash_api_key_sha256("correct")
        assert not verify_api_key("wrong", h, algorithm="sha256")

    def test_argon2_hashes_are_unique(self):
        raw = "same_key"
        h1  = hash_api_key_argon2(raw)
        h2  = hash_api_key_argon2(raw)
        # argon2 uses random salt, so hashes differ but both verify
        assert h1 != h2
        assert verify_api_key_argon2(raw, h1)
        assert verify_api_key_argon2(raw, h2)

    def test_dispatch_argon2(self):
        raw  = "test-key-dispatch"
        h    = hash_api_key_argon2(raw)
        assert verify_api_key(raw, h, algorithm="argon2")


# ─── Refresh token ────────────────────────────────────────────────────────────

class TestRefreshToken:
    def test_generate_returns_raw_and_hash(self):
        import hashlib
        raw, token_hash = generate_refresh_token()
        assert len(raw) > 20
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert token_hash == expected_hash

    def test_tokens_unique(self):
        raw1, _ = generate_refresh_token()
        raw2, _ = generate_refresh_token()
        assert raw1 != raw2


# ─── JWT ──────────────────────────────────────────────────────────────────────

class TestJWT:
    TENANT_ID = "tenant-abc-123"

    def test_access_token_has_required_claims(self):
        token = create_access_token("worker-1", self.TENANT_ID)
        # Decode without verification just to inspect claims
        payload = jose_jwt.decode(
            token, settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_aud": False},
        )
        assert payload["iss"] == JWT_ISSUER
        assert payload["sub"] == "worker-1"
        assert payload["tenant_id"] == self.TENANT_ID
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_audience_claim_matches_tenant(self):
        token = create_access_token("worker-1", self.TENANT_ID)
        payload = jose_jwt.decode(
            token, settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_aud": False},
        )
        assert payload["aud"] == f"ghostless:{self.TENANT_ID}"

    def test_decode_succeeds_with_correct_tenant(self):
        token   = create_access_token("w1", self.TENANT_ID)
        payload = decode_token(token, self.TENANT_ID)
        assert payload["sub"] == "w1"

    def test_decode_fails_wrong_tenant_audience(self):
        token = create_access_token("w1", self.TENANT_ID)
        with pytest.raises(JWTError):
            decode_token(token, "different-tenant")

    def test_decode_fails_expired_token(self):
        from datetime import timedelta
        from jose import jwt as jose_jwt
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        claims = {
            "iss": JWT_ISSUER,
            "aud": f"ghostless:{self.TENANT_ID}",
            "sub": "w1",
            "iat": past - timedelta(hours=1),
            "exp": past,  # already expired
            "jti": "test-jti",
            "tenant_id": self.TENANT_ID,
            "type": "worker",
        }
        expired_token = jose_jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
        with pytest.raises(JWTError):
            decode_token(expired_token, self.TENANT_ID)

    def test_per_tenant_secret_used(self):
        tenant_secret = "super-secret-per-tenant"
        token = create_access_token("w1", self.TENANT_ID, tenant_jwt_secret=tenant_secret)

        # Should decode with tenant secret
        payload = decode_token(token, self.TENANT_ID, tenant_jwt_secret=tenant_secret)
        assert payload["sub"] == "w1"

        # Should FAIL with global secret
        with pytest.raises(JWTError):
            decode_token(token, self.TENANT_ID, tenant_jwt_secret=None)

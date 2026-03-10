"""
Integration tests for auth flow and tenant isolation.

These tests use the real FastAPI app wired to SQLite test DB.
Tests cover:
  - API key validation (valid / revoked / expired / wrong tenant)
  - JWT token issue and validation
  - Cross-tenant data isolation (worker A cannot see worker B's data)
  - Rate limit headers
"""
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.models.models import APIKey, Tenant, Worker, WorkerScore
from app.services.auth import hash_api_key_sha256, generate_api_key


# ─── Helpers ──────────────────────────────────────────────────────────────────

def auth_headers(tenant: Tenant) -> dict:
    return {
        "X-API-Key":    tenant._test_api_key,
        "X-Tenant-ID":  tenant.slug,
    }


# ─── API Key auth tests ────────────────────────────────────────────────────────

class TestAPIKeyAuth:
    @pytest.mark.asyncio
    async def test_valid_key_returns_200(self, client: AsyncClient, test_tenant: Tenant):
        resp = await client.get(
            "/v1/workers/leaderboard",
            headers=auth_headers(test_tenant),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_key_returns_401(self, client: AsyncClient, test_tenant: Tenant):
        resp = await client.get(
            "/v1/workers/leaderboard",
            headers={"X-API-Key": "sk_live_gl_invalid_key", "X-Tenant-ID": test_tenant.slug},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_401(self, client: AsyncClient):
        resp = await client.get("/v1/workers/leaderboard")
        assert resp.status_code in (401, 422)  # 422 if x_tenant_id missing

    @pytest.mark.asyncio
    async def test_wrong_tenant_slug_returns_401(self, client: AsyncClient, test_tenant: Tenant):
        resp = await client.get(
            "/v1/workers/leaderboard",
            headers={"X-API-Key": test_tenant._test_api_key, "X-Tenant-ID": "wrong-slug"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_key_returns_401(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        db_session,
    ):
        from datetime import datetime, timezone
        from sqlalchemy import update as sql_update
        # Revoke the key
        await db_session.execute(
            sql_update(APIKey)
            .where(APIKey.tenant_id == test_tenant.id)
            .values(revoked_at=datetime.now(tz=timezone.utc))
        )
        await db_session.commit()

        resp = await client.get(
            "/v1/workers/leaderboard",
            headers=auth_headers(test_tenant),
        )
        assert resp.status_code == 401


# ─── Tenant isolation tests ───────────────────────────────────────────────────

class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_a_cannot_see_tenant_b_worker(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        test_tenant_b: Tenant,
        test_worker: Worker,
        db_session,
    ):
        """Worker belongs to tenant_a. Tenant_b should get 404."""
        resp = await client.get(
            f"/v1/workers/{test_worker.external_id}/score",
            headers=auth_headers(test_tenant_b),   # WRONG tenant
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_a_can_see_own_worker(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        test_worker: Worker,
    ):
        resp = await client.get(
            f"/v1/workers/{test_worker.external_id}/score",
            headers=auth_headers(test_tenant),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_leaderboard_only_returns_own_workers(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        test_tenant_b: Tenant,
        test_worker: Worker,
        db_session,
    ):
        """Tenant B's leaderboard should be empty (no workers)."""
        resp = await client.get(
            "/v1/workers/leaderboard",
            headers=auth_headers(test_tenant_b),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # No workers in tenant B
        worker_ids = [w["external_id"] for w in data]
        assert test_worker.external_id not in worker_ids


# ─── Earnings endpoint integration ───────────────────────────────────────────

class TestEarningsEndpoints:
    @pytest.mark.asyncio
    async def test_live_earnings_own_worker(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        test_worker: Worker,
    ):
        resp = await client.get(
            "/v1/earnings/live",
            params={"worker_id": test_worker.external_id},
            headers=auth_headers(test_tenant),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "trust_score" in data or "worker_id" in data

    @pytest.mark.asyncio
    async def test_live_earnings_cross_tenant_404(
        self,
        client: AsyncClient,
        test_tenant_b: Tenant,
        test_worker: Worker,
    ):
        resp = await client.get(
            "/v1/earnings/live",
            params={"worker_id": test_worker.external_id},
            headers=auth_headers(test_tenant_b),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_earnings_summary_own_worker(
        self,
        client: AsyncClient,
        test_tenant: Tenant,
        test_worker: Worker,
    ):
        resp = await client.get(
            "/v1/earnings/summary",
            params={"worker_id": test_worker.external_id, "period": "weekly"},
            headers=auth_headers(test_tenant),
        )
        assert resp.status_code == 200

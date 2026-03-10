"""
Shared test fixtures for Ghostless API test suite.

Uses SQLite (aiosqlite) for speed — no Docker required for unit/integration tests.
For full PostgreSQL tests, set TEST_DATABASE_URL=postgresql+asyncpg://... env var.
"""
import asyncio
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.models import (
    APIKey, Tenant, TenantTierEnum, Worker, WorkerScore, Task, Validation,
)
from app.services.auth import hash_api_key_sha256, generate_api_key

# ─── Test DB (SQLite in-memory) ───────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
TestSessionLocal = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client wired to the test DB."""
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ─── Fixture factories ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_tenant(db_session: AsyncSession) -> Tenant:
    raw_key, key_hash, key_prefix = generate_api_key()
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Test Tenant",
        slug=f"test-tenant-{uuid.uuid4().hex[:6]}",
        tier=TenantTierEnum.growth,
        is_active=True,
    )
    api_key = APIKey(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="test-key",
        key_hash=key_hash,
        hash_algorithm="sha256",   # use sha256 in tests for speed
        key_prefix=key_prefix,
        is_active=True,
    )
    # Store the raw key on the object for test use
    tenant._test_api_key = raw_key
    db_session.add(tenant)
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(tenant)
    return tenant


@pytest_asyncio.fixture
async def test_tenant_b(db_session: AsyncSession) -> Tenant:
    """Second tenant for isolation tests."""
    raw_key, key_hash, key_prefix = generate_api_key()
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Tenant B",
        slug=f"tenant-b-{uuid.uuid4().hex[:6]}",
        tier=TenantTierEnum.starter,
        is_active=True,
    )
    api_key = APIKey(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="b-key",
        key_hash=key_hash,
        hash_algorithm="sha256",
        key_prefix=key_prefix,
        is_active=True,
    )
    tenant._test_api_key = raw_key
    db_session.add(tenant)
    db_session.add(api_key)
    await db_session.commit()
    return tenant


@pytest_asyncio.fixture
async def test_worker(db_session: AsyncSession, test_tenant: Tenant) -> Worker:
    worker = Worker(
        id=uuid.uuid4(),
        tenant_id=test_tenant.id,
        external_id="worker-001",
        status="active",
    )
    score = WorkerScore(
        worker_id=worker.id,
        tenant_id=test_tenant.id,
        trust_score=55.0,
        ewma_quality=0.55,
        total_tasks=10,
        accepted_tasks=8,
        streak_days=3,
    )
    db_session.add(worker)
    db_session.add(score)
    await db_session.commit()
    return worker

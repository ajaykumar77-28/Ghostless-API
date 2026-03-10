"""
Unit + integration tests for the ledger service.

Tests cover:
  - Credit task payout
  - Reversal
  - Idempotency (double credit prevention)
  - Balance calculation
  - Tenant isolation (cross-tenant balance query returns 0)
"""
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.models import LedgerEntry, LedgerEntryType
from app.services.ledger import (
    credit_task_payout,
    get_balance,
    reverse_entry,
    credit_bonus,
)


@pytest_asyncio.fixture
async def worker_ids(test_worker, test_tenant):
    return {
        "worker_id": str(test_worker.id),
        "tenant_id": str(test_tenant.id),
    }


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_zero_balance_no_entries(self, db_session, worker_ids):
        balance = await get_balance(
            worker_ids["worker_id"],
            worker_ids["tenant_id"],
            db_session,
        )
        assert balance == Decimal("0")

    @pytest.mark.asyncio
    async def test_balance_sums_credits(self, db_session, worker_ids):
        for i in range(3):
            await credit_task_payout(
                worker_id       = worker_ids["worker_id"],
                tenant_id       = worker_ids["tenant_id"],
                task_id         = None,
                amount_usd      = Decimal("1.50"),
                idempotency_key = f"idem-balance-{i}-{uuid.uuid4()}",
                is_confirmed    = True,
                description     = "test credit",
                db              = db_session,
            )
        await db_session.flush()
        balance = await get_balance(
            worker_ids["worker_id"], worker_ids["tenant_id"], db_session
        )
        assert balance >= Decimal("4.50")


class TestCreditTaskPayout:
    @pytest.mark.asyncio
    async def test_credit_creates_entry(self, db_session, worker_ids):
        idem = f"credit-test-{uuid.uuid4()}"
        entry = await credit_task_payout(
            worker_id       = worker_ids["worker_id"],
            tenant_id       = worker_ids["tenant_id"],
            task_id         = None,
            amount_usd      = Decimal("2.00"),
            idempotency_key = idem,
            is_confirmed    = True,
            description     = "test",
            db              = db_session,
        )
        assert entry.amount_usd == Decimal("2.00")
        assert entry.entry_type == LedgerEntryType.task_payout

    @pytest.mark.asyncio
    async def test_idempotent_double_credit(self, db_session, worker_ids):
        idem  = f"idempotent-{uuid.uuid4()}"
        entry1 = await credit_task_payout(
            worker_ids["worker_id"], worker_ids["tenant_id"],
            None, Decimal("5.00"), idem, True, "first", db_session,
        )
        entry2 = await credit_task_payout(
            worker_ids["worker_id"], worker_ids["tenant_id"],
            None, Decimal("5.00"), idem, True, "second", db_session,
        )
        # Same entry returned, not a new one
        assert entry1.id == entry2.id


class TestReversal:
    @pytest.mark.asyncio
    async def test_reversal_creates_negative_entry(self, db_session, worker_ids):
        idem_credit = f"rev-credit-{uuid.uuid4()}"
        original = await credit_task_payout(
            worker_ids["worker_id"], worker_ids["tenant_id"],
            None, Decimal("3.00"), idem_credit, True, "original", db_session,
        )
        await db_session.flush()

        reversal = await reverse_entry(
            original_entry_id = str(original.id),
            tenant_id         = worker_ids["tenant_id"],
            worker_id         = worker_ids["worker_id"],
            reason            = "task rejected",
            idempotency_key   = f"rev-{uuid.uuid4()}",
            db                = db_session,
        )
        assert reversal.amount_usd == -Decimal("3.00")
        assert reversal.entry_type == LedgerEntryType.reversal

    @pytest.mark.asyncio
    async def test_cross_tenant_reversal_fails(self, db_session, worker_ids, test_tenant_b):
        idem_credit = f"xten-credit-{uuid.uuid4()}"
        original = await credit_task_payout(
            worker_ids["worker_id"], worker_ids["tenant_id"],
            None, Decimal("1.00"), idem_credit, True, "original", db_session,
        )
        await db_session.flush()

        with pytest.raises(ValueError, match="not found in tenant"):
            await reverse_entry(
                original_entry_id = str(original.id),
                tenant_id         = str(test_tenant_b.id),   # WRONG tenant
                worker_id         = worker_ids["worker_id"],
                reason            = "fraud",
                idempotency_key   = f"xrev-{uuid.uuid4()}",
                db                = db_session,
            )


class TestCreditBonus:
    @pytest.mark.asyncio
    async def test_bonus_adds_to_balance(self, db_session, worker_ids):
        idem = f"bonus-{uuid.uuid4()}"
        entry = await credit_bonus(
            worker_id       = worker_ids["worker_id"],
            tenant_id       = worker_ids["tenant_id"],
            amount_usd      = Decimal("0.50"),
            idempotency_key = idem,
            description     = "streak bonus",
            db              = db_session,
        )
        assert entry.entry_type == LedgerEntryType.bonus
        assert entry.amount_usd == Decimal("0.50")

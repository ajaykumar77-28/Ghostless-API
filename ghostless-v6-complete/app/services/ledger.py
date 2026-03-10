"""
Ghostless API — Ledger Service (v5)

New in v5:
  - Currency support (USD, EUR, GBP, CAD, AUD)
  - Clawback support (post-fraud review)
  - Negative balance protection
  - Duplicate payout detection via idempotency keys
  - Payout idempotency enforced on every write path
  - Immutable entries: all operations append; no UPDATE ever
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.models import LedgerEntry, LedgerEntryType


# ─── Balance helpers ──────────────────────────────────────────────────────────

async def get_balance(
    worker_id: str,
    tenant_id: str,
    db: AsyncSession,
    confirmed_only: bool = False,
    currency: str = "USD",
) -> Decimal:
    """Returns the worker's current balance for the given currency."""
    q = (
        select(func.coalesce(func.sum(LedgerEntry.amount_usd), Decimal("0")))
        .where(LedgerEntry.worker_id == worker_id)
        .where(LedgerEntry.tenant_id == tenant_id)
        .where(LedgerEntry.currency  == currency)
    )
    if confirmed_only:
        q = q.where(LedgerEntry.is_confirmed == True)
    result = await db.execute(q)
    return result.scalar() or Decimal("0")


async def _check_negative_guard(
    current_balance: Decimal,
    debit_amount: Decimal,
    limit: float = None,
) -> bool:
    """
    Returns True if the debit is allowed. False if it would breach the floor.
    limit = None uses settings.NEGATIVE_BALANCE_LIMIT (default 0.0).
    """
    floor = Decimal(str(limit if limit is not None else settings.NEGATIVE_BALANCE_LIMIT))
    return (current_balance - debit_amount) >= floor


async def _get_or_return_existing(
    idempotency_key: str,
    db: AsyncSession,
) -> Optional[LedgerEntry]:
    """Check idempotency key — return existing entry if found."""
    result = await db.execute(
        select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


# ─── Core operations ──────────────────────────────────────────────────────────

async def credit_task_payout(
    worker_id:       str,
    tenant_id:       str,
    task_id:         str,
    amount_usd:      Decimal,
    idempotency_key: str,
    is_confirmed:    bool,
    description:     str,
    db:              AsyncSession,
    currency:        str = "USD",
) -> LedgerEntry:
    """
    Credit a task payout. Idempotent — returns existing if key already exists.
    MUST be called inside an active transaction.
    """
    existing = await _get_or_return_existing(idempotency_key, db)
    if existing:
        return existing

    current_balance = await get_balance(worker_id, tenant_id, db, currency=currency)
    new_balance     = current_balance + amount_usd

    entry = LedgerEntry(
        id              = uuid.uuid4(),
        tenant_id       = tenant_id,
        worker_id       = worker_id,
        task_id         = task_id,
        entry_type      = LedgerEntryType.task_payout,
        amount_usd      = amount_usd,
        currency        = currency,
        running_balance = new_balance,
        description     = description,
        idempotency_key = idempotency_key,
        is_confirmed    = is_confirmed,
    )
    db.add(entry)
    return entry


async def reverse_entry(
    original_entry_id: str,
    tenant_id:         str,
    worker_id:         str,
    reason:            str,
    idempotency_key:   str,
    db:                AsyncSession,
) -> LedgerEntry:
    """Reverse a prior entry. Creates a new negative entry. Idempotent."""
    existing = await _get_or_return_existing(idempotency_key, db)
    if existing:
        return existing

    orig_result = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.id == original_entry_id)
        .where(LedgerEntry.tenant_id == tenant_id)
    )
    original = orig_result.scalar_one_or_none()
    if not original:
        raise ValueError(f"Original entry {original_entry_id} not found in tenant {tenant_id}")

    current_balance = await get_balance(worker_id, tenant_id, db, currency=original.currency)
    new_balance     = current_balance - original.amount_usd

    reversal = LedgerEntry(
        id              = uuid.uuid4(),
        tenant_id       = tenant_id,
        worker_id       = worker_id,
        task_id         = original.task_id,
        entry_type      = LedgerEntryType.reversal,
        amount_usd      = -original.amount_usd,
        currency        = original.currency,
        running_balance = new_balance,
        description     = f"Reversal of {original_entry_id}: {reason}",
        idempotency_key = idempotency_key,
        is_confirmed    = True,
        reference_id    = original.id,
    )
    db.add(reversal)
    return reversal


async def clawback_entry(
    original_entry_id: str,
    tenant_id:         str,
    worker_id:         str,
    reason:            str,
    idempotency_key:   str,
    db:                AsyncSession,
) -> LedgerEntry:
    """
    Clawback a payout after fraud review.
    Like reversal but entry_type = clawback, and negative balance protection applies.
    """
    existing = await _get_or_return_existing(idempotency_key, db)
    if existing:
        return existing

    orig_result = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.id == original_entry_id)
        .where(LedgerEntry.tenant_id == tenant_id)
    )
    original = orig_result.scalar_one_or_none()
    if not original:
        raise ValueError(f"Clawback target {original_entry_id} not found in tenant {tenant_id}")

    current_balance = await get_balance(worker_id, tenant_id, db, currency=original.currency)
    new_balance     = current_balance - original.amount_usd

    # Negative balance protection
    if not await _check_negative_guard(current_balance, original.amount_usd):
        raise ValueError(
            f"Clawback of {original.amount_usd} {original.currency} would breach balance floor "
            f"(current: {current_balance}, floor: {settings.NEGATIVE_BALANCE_LIMIT})"
        )

    clawback = LedgerEntry(
        id              = uuid.uuid4(),
        tenant_id       = tenant_id,
        worker_id       = worker_id,
        task_id         = original.task_id,
        entry_type      = LedgerEntryType.clawback,
        amount_usd      = -original.amount_usd,
        currency        = original.currency,
        running_balance = new_balance,
        description     = f"Clawback of {original_entry_id}: {reason}",
        idempotency_key = idempotency_key,
        clawback_reason = reason,
        is_confirmed    = True,
        reference_id    = original.id,
    )
    db.add(clawback)
    return clawback


async def credit_bonus(
    worker_id:       str,
    tenant_id:       str,
    amount_usd:      Decimal,
    idempotency_key: str,
    description:     str,
    db:              AsyncSession,
    currency:        str = "USD",
) -> LedgerEntry:
    """Credit a bonus payment atomically."""
    existing = await _get_or_return_existing(idempotency_key, db)
    if existing:
        return existing

    current_balance = await get_balance(worker_id, tenant_id, db, currency=currency)

    entry = LedgerEntry(
        id              = uuid.uuid4(),
        tenant_id       = tenant_id,
        worker_id       = worker_id,
        entry_type      = LedgerEntryType.bonus,
        amount_usd      = amount_usd,
        currency        = currency,
        running_balance = current_balance + amount_usd,
        description     = description,
        idempotency_key = idempotency_key,
        is_confirmed    = True,
    )
    db.add(entry)
    return entry

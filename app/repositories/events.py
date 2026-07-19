from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventType, Merchant, PaymentEvent, PaymentStatus, SettlementStatus, Transaction

_TRANSACTION_MUTABLE_COLUMNS = (
    "merchant_id", "amount", "currency", "payment_status", "settlement_status",
    "first_event_at", "last_event_at", "last_event_type", "settled_at",
    "is_discrepant", "discrepancy_reason",
)


async def lock_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> None:
    """Serialize all concurrent event processing for one transaction_id.

    A plain row lock (SELECT ... FOR UPDATE) isn't available when the
    transaction doesn't exist yet -- e.g. two concurrent payment_initiated
    events racing to create it. An advisory lock keyed on the transaction_id
    works regardless of whether the row exists, and is released automatically
    at commit/rollback (pg_advisory_xact_lock, not the session-scoped variant).
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:tid))"), {"tid": str(transaction_id)})


async def get_or_create_merchant_id(session: AsyncSession, merchant_code: str, merchant_name: str) -> uuid.UUID:
    """INSERT ... ON CONFLICT DO UPDATE (not DO NOTHING) so RETURNING fires
    whether the merchant is new or already existed -- one round trip either way.
    """
    stmt = (
        pg_insert(Merchant)
        .values(merchant_code=merchant_code, merchant_name=merchant_name)
        .on_conflict_do_update(index_elements=["merchant_code"], set_={"merchant_code": merchant_code})
        .returning(Merchant.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> Transaction | None:
    result = await session.execute(select(Transaction).where(Transaction.id == transaction_id))
    return result.scalar_one_or_none()


async def insert_payment_event(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    event_type: EventType,
    transaction_id: uuid.UUID,
    merchant_id: uuid.UUID,
    amount: Decimal,
    currency: str,
    event_timestamp: datetime,
    is_applied: bool,
) -> bool:
    """Returns True if a new row was inserted, False if event_id already
    existed (ON CONFLICT DO NOTHING) -- this is the duplicate-detection check.
    """
    stmt = (
        pg_insert(PaymentEvent)
        .values(
            event_id=event_id,
            event_type=event_type,
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            amount=amount,
            currency=currency,
            event_timestamp=event_timestamp,
            is_applied=is_applied,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(PaymentEvent.id)
    )
    result = await session.execute(stmt)
    return result.first() is not None


async def upsert_transaction(session: AsyncSession, fields: dict) -> tuple[
    uuid.UUID, PaymentStatus, SettlementStatus, str | None
]:
    """INSERT the transaction if this is its first event, otherwise update it
    in place -- one statement covers both cases. Returns just the columns the
    service needs for the response (not a full ORM instance: Core-level
    RETURNING on an ON CONFLICT statement yields plain columns, not mapped
    entities).
    """
    stmt = pg_insert(Transaction).values(**fields)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={col: getattr(stmt.excluded, col) for col in _TRANSACTION_MUTABLE_COLUMNS},
    ).returning(Transaction.id, Transaction.payment_status, Transaction.settlement_status, Transaction.discrepancy_reason)

    result = await session.execute(stmt)
    row = result.one()
    return row.id, row.payment_status, row.settlement_status, row.discrepancy_reason

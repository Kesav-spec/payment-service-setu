from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import Row, and_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant, PaymentEvent, PaymentStatus, SettlementStatus, Transaction

# Allow-list mapping sort_by -> (column, cursor-value parser). Never
# interpolate a client-supplied string into ORDER BY -- this dict is what
# makes "sort by anything" safe. Each column is paired with the parser that
# turns its cursor's encoded str(...) back into a typed value for the
# row-wise comparison below.
_SORT_COLUMNS = {
    "created_at": (Transaction.created_at, datetime.fromisoformat),
    "first_event_at": (Transaction.first_event_at, datetime.fromisoformat),
    "amount": (Transaction.amount, Decimal),
}


async def resolve_merchant_id(session: AsyncSession, merchant_code: str) -> uuid.UUID | None:
    result = await session.execute(select(Merchant.id).where(Merchant.merchant_code == merchant_code))
    return result.scalar_one_or_none()


async def get_transaction_with_merchant(session: AsyncSession, transaction_id: uuid.UUID) -> Row | None:
    stmt = (
        select(
            Transaction.id,
            Transaction.amount,
            Transaction.currency,
            Transaction.payment_status,
            Transaction.settlement_status,
            Transaction.first_event_at,
            Transaction.last_event_at,
            Transaction.last_event_type,
            Transaction.settled_at,
            Transaction.is_discrepant,
            Transaction.discrepancy_reason,
            Merchant.merchant_code,
            Merchant.merchant_name,
        )
        .join(Merchant, Merchant.id == Transaction.merchant_id)
        .where(Transaction.id == transaction_id)
    )
    result = await session.execute(stmt)
    return result.first()


async def list_transaction_events(session: AsyncSession, transaction_id: uuid.UUID) -> Sequence[Row]:
    """Ordered event history for one transaction. ix_payment_events_transaction_ts
    (transaction_id, event_timestamp) locates the matching rows via an index
    scan either way -- never a table scan; per EXPLAIN ANALYZE, Postgres
    picks a Bitmap Heap Scan + a small in-memory sort over an ordered Index
    Scan here, since a transaction's event count is tiny (a handful of
    lifecycle events) and the sort is effectively free at that size either way.
    """
    stmt = (
        select(
            PaymentEvent.event_id,
            PaymentEvent.event_type,
            PaymentEvent.amount,
            PaymentEvent.currency,
            PaymentEvent.event_timestamp,
            PaymentEvent.is_applied,
        )
        .where(PaymentEvent.transaction_id == transaction_id)
        .order_by(PaymentEvent.event_timestamp.asc())
    )
    result = await session.execute(stmt)
    return result.all()


async def list_transactions(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID | None,
    payment_status: PaymentStatus | None,
    settlement_status: SettlementStatus | None,
    is_discrepant: bool | None,
    from_date: datetime | None,
    to_date: datetime | None,
    sort_by: str,
    sort_dir: str,
    cursor: tuple[str, uuid.UUID] | None,
    limit: int,
) -> Sequence[Row]:
    """One query: filters as WHERE, sort_by/sort_dir as ORDER BY, and the
    cursor itself as a row-wise WHERE comparison -- (sort_column, id) <or> (cursor)
    -- which Postgres can seek directly via an index on the sort column
    instead of scanning past skipped rows the way OFFSET would. Only
    created_at has a supporting (sort_column, id) index today
    (ix_transactions_created_at_id); first_event_at/amount still filter and
    sort correctly, just without that same index-seek benefit.
    """
    conditions = []
    if merchant_id is not None:
        conditions.append(Transaction.merchant_id == merchant_id)
    if payment_status is not None:
        conditions.append(Transaction.payment_status == payment_status)
    if settlement_status is not None:
        conditions.append(Transaction.settlement_status == settlement_status)
    if is_discrepant is not None:
        conditions.append(Transaction.is_discrepant == is_discrepant)
    if from_date is not None:
        conditions.append(Transaction.first_event_at >= from_date)
    if to_date is not None:
        conditions.append(Transaction.first_event_at <= to_date)

    sort_column, parse_cursor_value = _SORT_COLUMNS[sort_by]
    if cursor is not None:
        cursor_value_str, cursor_id = cursor
        cursor_value = parse_cursor_value(cursor_value_str)
        row = tuple_(sort_column, Transaction.id)
        cursor_row = tuple_(cursor_value, cursor_id)
        conditions.append(row > cursor_row if sort_dir == "asc" else row < cursor_row)

    stmt = (
        select(
            Transaction.id,
            Merchant.merchant_code,
            Transaction.amount,
            Transaction.currency,
            Transaction.payment_status,
            Transaction.settlement_status,
            Transaction.first_event_at,
            Transaction.last_event_at,
            Transaction.last_event_type,
            Transaction.settled_at,
            Transaction.is_discrepant,
            Transaction.discrepancy_reason,
            Transaction.created_at,
        )
        .join(Merchant, Merchant.id == Transaction.merchant_id)
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))

    order = (sort_column.asc(), Transaction.id.asc()) if sort_dir == "asc" else (sort_column.desc(), Transaction.id.desc())
    stmt = stmt.order_by(*order).limit(limit)

    result = await session.execute(stmt)
    return result.all()

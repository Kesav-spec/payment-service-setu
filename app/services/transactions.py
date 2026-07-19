from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import encode_cursor
from app.models import PaymentStatus, SettlementStatus
from app.repositories import transactions as transactions_repo
from app.schemas import (
    MerchantOut,
    TransactionDetail,
    TransactionDetailResponse,
    TransactionEventOut,
    TransactionListResponse,
    TransactionOut,
)


async def list_transactions(
    session: AsyncSession,
    *,
    merchant_code: str | None,
    payment_status: PaymentStatus | None,
    settlement_status: SettlementStatus | None,
    is_discrepant: bool | None,
    from_date: datetime | None,
    to_date: datetime | None,
    sort_by: str,
    sort_dir: str,
    cursor: tuple[str, uuid.UUID] | None,
    limit: int,
) -> TransactionListResponse:
    merchant_id = None
    if merchant_code is not None:
        merchant_id = await transactions_repo.resolve_merchant_id(session, merchant_code)
        if merchant_id is None:
            # No such merchant -- the filter can never match, so skip the
            # main query entirely rather than run it against an impossible id.
            return TransactionListResponse(items=[], next_cursor=None, limit=limit)

    # Fetch one extra row -- its presence, not a COUNT query, is how we know
    # there's a next page. SQL still fully determines which rows and their
    # order; Python only inspects len(rows) and drops the lookahead row.
    rows = await transactions_repo.list_transactions(
        session,
        merchant_id=merchant_id,
        payment_status=payment_status,
        settlement_status=settlement_status,
        is_discrepant=is_discrepant,
        from_date=from_date,
        to_date=to_date,
        sort_by=sort_by,
        sort_dir=sort_dir,
        cursor=cursor,
        limit=limit + 1,
    )

    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = encode_cursor(getattr(page[-1], sort_by), page[-1].id) if has_more else None

    items = [
        TransactionOut(
            id=row.id,
            merchant_id=row.merchant_code,
            amount=row.amount,
            currency=row.currency,
            payment_status=row.payment_status,
            settlement_status=row.settlement_status,
            first_event_at=row.first_event_at,
            last_event_at=row.last_event_at,
            last_event_type=row.last_event_type,
            settled_at=row.settled_at,
            is_discrepant=row.is_discrepant,
            discrepancy_reason=row.discrepancy_reason,
        )
        for row in page
    ]
    return TransactionListResponse(items=items, next_cursor=next_cursor, limit=limit)


async def get_transaction_detail(session: AsyncSession, transaction_id: uuid.UUID) -> TransactionDetailResponse | None:
    # 2 queries total regardless of event count: one for transaction+merchant
    # (a single row), one for the ordered event history. Neither loops nor
    # issues a query per related row, so this never becomes N+1.
    row = await transactions_repo.get_transaction_with_merchant(session, transaction_id)
    if row is None:
        return None

    event_rows = await transactions_repo.list_transaction_events(session, transaction_id)

    return TransactionDetailResponse(
        transaction=TransactionDetail(
            id=row.id,
            amount=row.amount,
            currency=row.currency,
            payment_status=row.payment_status,
            settlement_status=row.settlement_status,
            first_event_at=row.first_event_at,
            last_event_at=row.last_event_at,
            last_event_type=row.last_event_type,
            settled_at=row.settled_at,
            is_discrepant=row.is_discrepant,
            discrepancy_reason=row.discrepancy_reason,
        ),
        merchant=MerchantOut(merchant_id=row.merchant_code, merchant_name=row.merchant_name),
        events=[
            TransactionEventOut(
                event_id=e.event_id,
                event_type=e.event_type,
                amount=e.amount,
                currency=e.currency,
                event_timestamp=e.event_timestamp,
                is_applied=e.is_applied,
            )
            for e in event_rows
        ],
    )

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import reconciliation as reconciliation_repo
from app.repositories import transactions as transactions_repo
from app.repositories.reconciliation import GroupBy
from app.schemas import DiscrepancyOut, ReconciliationGroupSummary


def _format_group_key(value: Any, group_by: GroupBy) -> str:
    if group_by == "date":
        return value.isoformat()
    if group_by == "status":
        return value.value
    return value  # merchant_code is already a plain str


async def get_summary(
    session: AsyncSession,
    *,
    merchant_code: str | None,
    from_date: dt.datetime | None,
    to_date: dt.datetime | None,
    group_by: GroupBy,
) -> list[ReconciliationGroupSummary]:
    merchant_id = None
    if merchant_code is not None:
        merchant_id = await transactions_repo.resolve_merchant_id(session, merchant_code)
        if merchant_id is None:
            # No such merchant -- there's nothing to group, so zero groups
            # (not one all-zero row: there's no "group" to represent).
            return []

    rows = await reconciliation_repo.get_summary(
        session,
        merchant_id=merchant_id,
        from_date=from_date,
        to_date=to_date,
        group_by=group_by,
    )

    return [
        ReconciliationGroupSummary(
            group_key=_format_group_key(row.group_key, group_by),
            total_transactions=row.total_transactions,
            total_amount=row.total_amount,
            initiated_count=row.initiated_count,
            initiated_amount=row.initiated_amount,
            processed_count=row.processed_count,
            processed_amount=row.processed_amount,
            failed_count=row.failed_count,
            failed_amount=row.failed_amount,
            settled_count=row.settled_count,
            settled_amount=row.settled_amount,
            unsettled_count=row.unsettled_count,
            unsettled_amount=row.unsettled_amount,
            discrepant_count=row.discrepant_count,
            discrepant_amount=row.discrepant_amount,
        )
        for row in rows
    ]


async def get_discrepancies(
    session: AsyncSession,
    *,
    merchant_code: str | None,
    stale_after_hours: int,
    limit: int,
) -> list[DiscrepancyOut]:
    merchant_id = None
    if merchant_code is not None:
        merchant_id = await transactions_repo.resolve_merchant_id(session, merchant_code)
        if merchant_id is None:
            # No such merchant -- nothing can match.
            return []

    stale_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=stale_after_hours)

    rows = await reconciliation_repo.get_discrepancies(
        session,
        merchant_id=merchant_id,
        stale_cutoff=stale_cutoff,
        limit=limit,
    )

    return [
        DiscrepancyOut(
            transaction_id=row.transaction_id,
            merchant_id=row.merchant_code,
            discrepancy_reason=row.discrepancy_reason,
            payment_status=row.payment_status,
            settlement_status=row.settlement_status,
            amount=row.amount,
            currency=row.currency,
            first_event_at=row.first_event_at,
            last_event_at=row.last_event_at,
        )
        for row in rows
    ]

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Sequence

from sqlalchemy import Date, Row, and_, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant, PaymentStatus, SettlementStatus, Transaction

GroupBy = Literal["merchant", "date", "status"]

# One expression per grouping dimension -- used as both the SELECT'd group
# key and the GROUP BY key, so "group by X" and "the column shown for X"
# can never drift apart. "date" is day-level granularity (UTC).
_GROUP_EXPRESSIONS = {
    "merchant": Merchant.merchant_code,
    "date": cast(Transaction.first_event_at, Date),
    "status": Transaction.payment_status,
}


async def get_summary(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID | None,
    from_date: datetime | None,
    to_date: datetime | None,
    group_by: GroupBy,
) -> Sequence[Row]:
    """Same one-pass conditional aggregation as before (FILTER + COALESCE for
    every payment/settlement bucket) -- now with one extra GROUP BY dimension
    and its corresponding group-key column selected alongside it. Grouping
    happens entirely in this single SQL statement; there is no second pass
    over the rows in Python.
    """
    conditions = []
    if merchant_id is not None:
        conditions.append(Transaction.merchant_id == merchant_id)
    if from_date is not None:
        conditions.append(Transaction.first_event_at >= from_date)
    if to_date is not None:
        conditions.append(Transaction.first_event_at <= to_date)

    def count_where(*preds):
        return func.count().filter(*preds)

    def sum_where(*preds):
        return func.coalesce(func.sum(Transaction.amount).filter(*preds), 0)

    group_expr = _GROUP_EXPRESSIONS[group_by]

    stmt = select(
        group_expr.label("group_key"),
        func.count().label("total_transactions"),
        func.coalesce(func.sum(Transaction.amount), 0).label("total_amount"),
        count_where(Transaction.payment_status == PaymentStatus.INITIATED).label("initiated_count"),
        sum_where(Transaction.payment_status == PaymentStatus.INITIATED).label("initiated_amount"),
        count_where(Transaction.payment_status == PaymentStatus.PROCESSED).label("processed_count"),
        sum_where(Transaction.payment_status == PaymentStatus.PROCESSED).label("processed_amount"),
        count_where(Transaction.payment_status == PaymentStatus.FAILED).label("failed_count"),
        sum_where(Transaction.payment_status == PaymentStatus.FAILED).label("failed_amount"),
        count_where(Transaction.settlement_status == SettlementStatus.SETTLED).label("settled_count"),
        sum_where(Transaction.settlement_status == SettlementStatus.SETTLED).label("settled_amount"),
        count_where(Transaction.settlement_status == SettlementStatus.UNSETTLED).label("unsettled_count"),
        sum_where(Transaction.settlement_status == SettlementStatus.UNSETTLED).label("unsettled_amount"),
        count_where(Transaction.is_discrepant.is_(True)).label("discrepant_count"),
        sum_where(Transaction.is_discrepant.is_(True)).label("discrepant_amount"),
    )

    if group_by == "merchant":
        stmt = stmt.join(Merchant, Merchant.id == Transaction.merchant_id)

    if conditions:
        stmt = stmt.where(and_(*conditions))

    stmt = stmt.group_by(group_expr).order_by(group_expr)

    result = await session.execute(stmt)
    return result.all()


def _discrepancy_columns(reason, merchant_id: uuid.UUID | None):
    """Shared SELECT column list for all three discrepancy branches --
    identical shape (transaction_id, merchant_code, reason, status pair,
    amount, currency, timestamps) so UNION ALL lines up column-for-column.
    reason is either Transaction.discrepancy_reason (branch A, where the
    real stored reason varies per row) or a literal string constant
    (branches B/C, which each represent exactly one reason).
    """
    cols = [
        Transaction.id.label("transaction_id"),
        Merchant.merchant_code.label("merchant_code"),
        reason.label("discrepancy_reason"),
        Transaction.payment_status,
        Transaction.settlement_status,
        Transaction.amount,
        Transaction.currency,
        Transaction.first_event_at,
        Transaction.last_event_at,
    ]
    stmt = select(*cols).join(Merchant, Merchant.id == Transaction.merchant_id)
    if merchant_id is not None:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    return stmt


async def get_discrepancies(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID | None,
    stale_cutoff: datetime,
    limit: int,
) -> Sequence[Row]:
    """Three branches, one per detection mechanism, combined with UNION ALL
    (not UNION -- a transaction that's both flagged-at-ingest and currently
    stale is two distinct findings, not one to be deduplicated away):

      A. is_discrepant = true -- everything the state machine already
         flagged at write time (conflicting_transitions, settled_after_failure,
         settled_before_processed, initiated_missing), via ix_transactions_discrepant.
      B. PROCESSED + UNSETTLED past stale_cutoff, via ix_transactions_processed_unsettled.
      C. still INITIATED past stale_cutoff -- no supporting index today.

    ORDER BY / LIMIT apply once, to the combined result -- one round trip,
    no Python-side merging of three separate result sets.
    """
    branch_a = _discrepancy_columns(Transaction.discrepancy_reason, merchant_id).where(
        Transaction.is_discrepant.is_(True)
    )
    branch_b = _discrepancy_columns(literal("processed_not_settled"), merchant_id).where(
        Transaction.payment_status == PaymentStatus.PROCESSED,
        Transaction.settlement_status == SettlementStatus.UNSETTLED,
        Transaction.last_event_at < stale_cutoff,
    )
    branch_c = _discrepancy_columns(literal("stuck_initiated"), merchant_id).where(
        Transaction.payment_status == PaymentStatus.INITIATED,
        Transaction.first_event_at < stale_cutoff,
    )

    combined = union_all(branch_a, branch_b, branch_c).subquery()
    stmt = select(combined).order_by(combined.c.last_event_at.desc()).limit(limit)

    result = await session.execute(stmt)
    return result.all()

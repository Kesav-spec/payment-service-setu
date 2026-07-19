from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.repositories.reconciliation import GroupBy
from app.schemas import DiscrepancyOut, ReconciliationGroupSummary
from app.services.reconciliation import get_discrepancies, get_summary

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("/summary", response_model=list[ReconciliationGroupSummary])
async def get_reconciliation_summary(
    group_by: GroupBy = Query(..., description="Grouping dimension: merchant, date, or status"),
    merchant_id: str | None = Query(None, description="Merchant code, e.g. 'merchant_2'"),
    from_date: datetime | None = Query(None, description="Inclusive lower bound on first_event_at"),
    to_date: datetime | None = Query(None, description="Inclusive upper bound on first_event_at"),
    session: AsyncSession = Depends(get_session),
) -> list[ReconciliationGroupSummary]:
    return await get_summary(
        session,
        merchant_code=merchant_id,
        from_date=from_date,
        to_date=to_date,
        group_by=group_by,
    )


@router.get("/discrepancies", response_model=list[DiscrepancyOut])
async def get_reconciliation_discrepancies(
    merchant_id: str | None = Query(None, description="Merchant code, e.g. 'merchant_2'"),
    stale_after_hours: int = Query(
        24, ge=1, description="Threshold for processed_not_settled / stuck_initiated staleness"
    ),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[DiscrepancyOut]:
    return await get_discrepancies(
        session,
        merchant_code=merchant_id,
        stale_after_hours=stale_after_hours,
        limit=limit,
    )

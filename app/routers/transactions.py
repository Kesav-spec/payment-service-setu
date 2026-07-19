import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.pagination import decode_cursor
from app.models import PaymentStatus, SettlementStatus
from app.schemas import TransactionDetailResponse, TransactionListResponse
from app.services.transactions import get_transaction_detail, list_transactions

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=TransactionListResponse)
async def get_transactions(
    merchant_id: str | None = Query(None, description="Merchant code, e.g. 'merchant_2'"),
    payment_status: PaymentStatus | None = Query(None),
    status: PaymentStatus | None = Query(None, description="Alias for payment_status"),
    settlement_status: SettlementStatus | None = Query(None),
    is_discrepant: bool | None = Query(None),
    from_date: datetime | None = Query(None, description="Inclusive lower bound on first_event_at"),
    to_date: datetime | None = Query(None, description="Inclusive upper bound on first_event_at"),
    sort_by: Literal["created_at", "first_event_at", "amount"] = Query("created_at"),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
    cursor: str | None = Query(None, description="Opaque cursor from a previous response's next_cursor"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> TransactionListResponse:
    decoded_cursor = None
    if cursor is not None:
        try:
            decoded_cursor = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid cursor")

    return await list_transactions(
        session,
        merchant_code=merchant_id,
        payment_status=status if status is not None else payment_status,
        settlement_status=settlement_status,
        is_discrepant=is_discrepant,
        from_date=from_date,
        to_date=to_date,
        sort_by=sort_by,
        sort_dir=sort_dir,
        cursor=decoded_cursor,
        limit=limit,
    )


@router.get("/{transaction_id}", response_model=TransactionDetailResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> TransactionDetailResponse:
    result = await get_transaction_detail(session, transaction_id)
    if result is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return result

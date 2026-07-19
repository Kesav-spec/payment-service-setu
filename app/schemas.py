from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models import EventType, PaymentStatus, SettlementStatus

# ---------------------------------------------------------------------------
# POST /events
# ---------------------------------------------------------------------------


class EventCreate(BaseModel):
    event_id: uuid.UUID
    event_type: EventType
    transaction_id: uuid.UUID

    # The source system's merchant *code* (e.g. "merchant_2"), matching
    # Merchant.merchant_code -- NOT the internal Transaction.merchant_id UUID.
    # Field name matches the wire payload verbatim; resolved to a UUID during
    # ingestion.
    merchant_id: str = Field(min_length=1, max_length=64)
    merchant_name: str = Field(min_length=1, max_length=255)

    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    # Business event time (not ingestion/arrival time). Must be timezone-aware
    # -- the state machine compares this directly against the transaction's
    # last_event_at, and a naive/aware comparison would raise at runtime.
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone offset")
        return v


class EventIngestResponse(BaseModel):
    event_id: uuid.UUID
    # "duplicate" = this exact event_id was already ingested before (DB-level
    # idempotency). is_applied separately reports whether the event changed
    # transaction state at all -- a distinct, business-level notion of
    # "no-op" (e.g. a second, differently-event_id'd settlement confirmation).
    status: Literal["accepted", "duplicate"]
    is_applied: bool

    transaction_id: uuid.UUID
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    discrepancy_reason: str | None


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------


class TransactionOut(BaseModel):
    id: uuid.UUID
    # Merchant *code* (e.g. "merchant_2"), not the internal UUID -- same wire
    # convention as EventCreate.merchant_id.
    merchant_id: str
    amount: Decimal
    currency: str
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    first_event_at: datetime
    last_event_at: datetime
    last_event_type: EventType
    settled_at: datetime | None
    is_discrepant: bool
    discrepancy_reason: str | None


class TransactionListResponse(BaseModel):
    items: list[TransactionOut]
    # Opaque, base64-encoded (created_at, id) cursor for the next page; null
    # when this is the last page. No total count -- see the cursor-pagination
    # tradeoffs in the GET /transactions implementation notes.
    next_cursor: str | None
    limit: int


# ---------------------------------------------------------------------------
# GET /transactions/{id}
# ---------------------------------------------------------------------------


class TransactionDetail(BaseModel):
    id: uuid.UUID
    amount: Decimal
    currency: str
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    first_event_at: datetime
    last_event_at: datetime
    last_event_type: EventType
    settled_at: datetime | None
    is_discrepant: bool
    discrepancy_reason: str | None


class MerchantOut(BaseModel):
    merchant_id: str  # merchant_code
    merchant_name: str


class TransactionEventOut(BaseModel):
    event_id: uuid.UUID
    event_type: EventType
    amount: Decimal
    currency: str
    event_timestamp: datetime
    is_applied: bool


class TransactionDetailResponse(BaseModel):
    transaction: TransactionDetail
    merchant: MerchantOut
    events: list[TransactionEventOut]


# ---------------------------------------------------------------------------
# GET /reconciliation/summary
# ---------------------------------------------------------------------------


class ReconciliationGroupSummary(BaseModel):
    # The dimension value this row represents -- a merchant_code, an ISO
    # "YYYY-MM-DD" date, or a payment_status value, depending on the
    # request's group_by. The caller already knows which, since they chose
    # group_by; each row doesn't repeat it.
    group_key: str

    total_transactions: int
    total_amount: Decimal

    initiated_count: int
    initiated_amount: Decimal
    processed_count: int
    processed_amount: Decimal
    failed_count: int
    failed_amount: Decimal

    settled_count: int
    settled_amount: Decimal
    unsettled_count: int
    unsettled_amount: Decimal

    discrepant_count: int
    discrepant_amount: Decimal


# ---------------------------------------------------------------------------
# GET /reconciliation/discrepancies
# ---------------------------------------------------------------------------


class DiscrepancyOut(BaseModel):
    transaction_id: uuid.UUID
    merchant_id: str  # merchant_code
    discrepancy_reason: str
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    amount: Decimal
    currency: str
    first_event_at: datetime
    last_event_at: datetime

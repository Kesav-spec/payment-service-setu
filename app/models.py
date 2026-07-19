from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, Numeric, String, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

# ---------------------------------------------------------------------------
# Enums
#
# Backed by native Postgres ENUM types (not plain strings) so invalid values are
# rejected at the database layer too, not just by Pydantic at the API boundary.
# `values_callable` forces SQLAlchemy to persist `.value` rather than the Python
# member `.name` -- required for EventType, where they intentionally differ
# (member PAYMENT_INITIATED, value "payment_initiated") to match the source
# event payload verbatim.
# ---------------------------------------------------------------------------


class EventType(str, enum.Enum):
    PAYMENT_INITIATED = "payment_initiated"
    PAYMENT_PROCESSED = "payment_processed"
    PAYMENT_FAILED = "payment_failed"
    SETTLED = "settled"


class PaymentStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class SettlementStatus(str, enum.Enum):
    UNSETTLED = "UNSETTLED"
    SETTLED = "SETTLED"


def _enum_values(enum_cls: type[enum.Enum]):
    return [member.value for member in enum_cls]


# ---------------------------------------------------------------------------
# Merchant
# ---------------------------------------------------------------------------


class Merchant(Base):
    __tablename__ = "merchants"

    # Surrogate UUID PK, decoupled from the source system's merchant identifier.
    # The sample payload's merchant_id ("merchant_2") is a slug, not a UUID, so it
    # is stored separately as `merchant_code` (unique, indexed) rather than forced
    # into the PK. `default` generates client-side (portable, works pre-flush);
    # `server_default` guarantees a value even for rows inserted outside the ORM
    # (bulk COPY, raw SQL) -- both point at the same generation strategy so a
    # value is never missing regardless of insert path.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    merchant_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()"), nullable=False
    )

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="merchant")
    payment_events: Mapped[list["PaymentEvent"]] = relationship(back_populates="merchant")


# ---------------------------------------------------------------------------
# Transaction
#
# This is the current-state projection of a transaction's event stream -- one
# row per transaction_id, kept up to date by replaying payment_events through
# the state machine (implemented at the service layer, not here). It is fully
# reconstructable from payment_events, which is what makes idempotent replay
# and late/out-of-order event handling safe.
# ---------------------------------------------------------------------------


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Covers "filter by merchant" and "filter by merchant + status" -- the
        # two most common shapes of GET /transactions -- with one index (status
        # alone can use this as a prefix-less scan too via bitmap/index skip,
        # but the primary win is the combined filter).
        Index("ix_transactions_merchant_status", "merchant_id", "payment_status"),
        # Covers "filter by merchant + date range, sorted by date" -- the other
        # common GET /transactions shape -- and supports keyset pagination
        # directly off the index (no separate sort step).
        Index("ix_transactions_merchant_first_event_at", "merchant_id", "first_event_at"),
        # Partial indexes for the two known discrepancy shapes: each targets a
        # narrow slice of a large table, so it stays small and cheap to
        # maintain regardless of overall table size.
        Index(
            "ix_transactions_processed_unsettled",
            "payment_status",
            "settlement_status",
            postgresql_where=text("payment_status = 'PROCESSED' AND settlement_status = 'UNSETTLED'"),
        ),
        Index(
            "ix_transactions_failed_settled",
            "payment_status",
            "settlement_status",
            postgresql_where=text("payment_status = 'FAILED' AND settlement_status = 'SETTLED'"),
        ),
        # Backs GET /reconciliation/discrepancies directly once a row has been
        # classified (by the service layer) as discrepant for any reason.
        Index(
            "ix_transactions_discrepant",
            "is_discrepant",
            postgresql_where=text("is_discrepant = true"),
        ),
        # Cursor key for GET /transactions keyset pagination: created_at alone
        # can tie (bulk-loaded rows can share a timestamp), so id is a second
        # column, not just an ORDER BY tiebreaker computed after the fact --
        # the composite index is what lets the (created_at, id) < (cursor)
        # row comparison seek directly instead of scanning.
        Index("ix_transactions_created_at_id", "created_at", "id"),
    )

    # Deliberately NOT server- or client-generated: this is the transaction_id
    # supplied by the source event payload. Using it directly as the PK (rather
    # than inventing a separate surrogate key) means no lookup/translation step
    # is needed when events reference a transaction.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status", values_callable=_enum_values),
        nullable=False,
    )
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        SAEnum(SettlementStatus, name="settlement_status", values_callable=_enum_values),
        nullable=False,
        default=SettlementStatus.UNSETTLED,
        server_default=SettlementStatus.UNSETTLED.value,
    )

    # Business timestamps (from event payloads), distinct from created_at/updated_at
    # (row bookkeeping timestamps below). Used by the state machine to detect
    # out-of-order/late events and by /reconciliation SLA checks.
    first_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Denormalized so the transaction list/detail views don't need to join
    # payment_events just to show "what happened last." Reuses the same
    # Postgres ENUM type as PaymentEvent.event_type (one type, two columns).
    last_event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type", values_callable=_enum_values),
        nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Maintained by the service layer on every applied event, so
    # /reconciliation/discrepancies is a plain indexed read rather than a
    # computed-on-request scan/join.
    is_discrepant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    discrepancy_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()"), nullable=False
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="transactions")
    events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="transaction", order_by="PaymentEvent.event_timestamp"
    )


# ---------------------------------------------------------------------------
# PaymentEvent
#
# Append-only ledger: rows are inserted, never updated or deleted. This is the
# system of record; `transactions` is a derived cache of it.
# ---------------------------------------------------------------------------


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        # Fetch a transaction's history pre-sorted (transaction_id is the
        # leading column, event_timestamp lets Postgres satisfy ORDER BY
        # straight from the index -- no separate sort step).
        Index("ix_payment_events_transaction_ts", "transaction_id", "event_timestamp"),
        # Same reasoning for merchant-scoped time-bucketed queries (e.g. the
        # "date" and "merchant" dimensions of /reconciliation/summary).
        Index("ix_payment_events_merchant_ts", "merchant_id", "event_timestamp"),
    )

    # Surrogate, insertion-ordered PK. Cheap to generate on a write-heavy,
    # append-only table (`Identity()` -> GENERATED BY DEFAULT AS IDENTITY, the
    # modern replacement for SERIAL) and never used as a business key --
    # `event_id` is the one callers/consumers reference.
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # The idempotency key from the source system. This UNIQUE constraint is the
    # actual enforcement mechanism for "duplicate submission must not corrupt
    # state": ingestion does INSERT ... ON CONFLICT (event_id) DO NOTHING and
    # treats 0-rows-affected as "already processed", entirely at the DB level
    # rather than a SELECT-then-check race in application code.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)

    event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type", values_callable=_enum_values),
        nullable=False,
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False
    )
    # Denormalized from transaction.merchant_id: lets merchant-scoped event
    # queries (reconciliation summaries grouped by merchant+date) avoid joining
    # through transactions. Consistency is guaranteed by the ingestion service
    # (always written from the same event payload as transaction_id), not by a
    # DB-level cross-table constraint.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Business time from the payload -- what the state machine orders on.
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Ingestion (arrival) time -- operational/debugging use only, never used
    # for ordering business state.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)

    # False when the event was stored (preserving history) but did not change
    # transaction state: a duplicate, a late/out-of-order arrival, or an
    # invalid transition (e.g. payment_processed after payment_failed). Lets
    # /reconciliation/discrepancies' "conflicting_transitions" case be a plain
    # filter on this column instead of re-deriving it from timestamps at read time.
    is_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)

    transaction: Mapped["Transaction"] = relationship(back_populates="events")
    merchant: Mapped["Merchant"] = relationship(back_populates="payment_events")

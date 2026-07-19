"""initial schema: merchants, transactions, payment_events

Revision ID: d74559a56dee
Revises:
Create Date: 2026-07-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d74559a56dee"
down_revision = None
branch_labels = None
depends_on = None

# Native Postgres ENUM types. `event_type` is shared by two columns
# (transactions.last_event_type and payment_events.event_type), so it is
# created exactly once, explicitly, below -- every column reference uses
# create_type=False so CREATE TABLE never tries to (re)create it and race
# with itself.
payment_status_enum = postgresql.ENUM(
    "INITIATED", "PROCESSED", "FAILED", name="payment_status", create_type=False
)
settlement_status_enum = postgresql.ENUM(
    "UNSETTLED", "SETTLED", name="settlement_status", create_type=False
)
event_type_enum = postgresql.ENUM(
    "payment_initiated",
    "payment_processed",
    "payment_failed",
    "settled",
    name="event_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    payment_status_enum.create(bind, checkfirst=True)
    settlement_status_enum.create(bind, checkfirst=True)
    event_type_enum.create(bind, checkfirst=True)

    # -- merchants ------------------------------------------------------
    # Surrogate UUID PK (gen_random_uuid(), built into Postgres 13+, no
    # extension required) decoupled from the source system's merchant
    # identifier, which is a slug ("merchant_2") and stored separately.
    op.create_table(
        "merchants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_code", sa.String(length=64), nullable=False),
        sa.Column("merchant_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_merchants"),
        sa.UniqueConstraint("merchant_code", name="uq_merchants_merchant_code"),
    )

    # -- transactions -----------------------------------------------------
    # PK is the transaction_id supplied by the source event payload (not
    # generated here) -- this is the current-state projection of a
    # transaction's event stream, reconstructable by replaying payment_events.
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_status", payment_status_enum, nullable=False),
        sa.Column(
            "settlement_status",
            settlement_status_enum,
            server_default="UNSETTLED",
            nullable=False,
        ),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_type", event_type_enum, nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_discrepant", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("discrepancy_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_transactions_merchant_id_merchants",
            ondelete="RESTRICT",
        ),
    )
    # Filter shapes for GET /transactions.
    op.create_index(
        "ix_transactions_merchant_status", "transactions", ["merchant_id", "payment_status"]
    )
    op.create_index(
        "ix_transactions_merchant_first_event_at", "transactions", ["merchant_id", "first_event_at"]
    )
    # Partial indexes targeting the two known reconciliation-discrepancy
    # shapes -- narrow slices of the table, cheap regardless of table size.
    op.create_index(
        "ix_transactions_processed_unsettled",
        "transactions",
        ["payment_status", "settlement_status"],
        postgresql_where=sa.text("payment_status = 'PROCESSED' AND settlement_status = 'UNSETTLED'"),
    )
    op.create_index(
        "ix_transactions_failed_settled",
        "transactions",
        ["payment_status", "settlement_status"],
        postgresql_where=sa.text("payment_status = 'FAILED' AND settlement_status = 'SETTLED'"),
    )
    # Backs GET /reconciliation/discrepancies once a row is classified.
    op.create_index(
        "ix_transactions_discrepant",
        "transactions",
        ["is_discrepant"],
        postgresql_where=sa.text("is_discrepant = true"),
    )

    # -- payment_events ---------------------------------------------------
    # Append-only ledger; system of record. `id` is a surrogate identity PK
    # (never a business key); `event_id` is the source system's idempotency
    # key -- its UNIQUE constraint is the actual duplicate-submission guard.
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", event_type_enum, nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_applied", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payment_events"),
        sa.UniqueConstraint("event_id", name="uq_payment_events_event_id"),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_payment_events_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_payment_events_merchant_id_merchants",
            ondelete="RESTRICT",
        ),
    )
    # Pre-sorted history lookups: leading column narrows to one
    # transaction/merchant, trailing timestamp column satisfies ORDER BY
    # straight from the index.
    op.create_index(
        "ix_payment_events_transaction_ts", "payment_events", ["transaction_id", "event_timestamp"]
    )
    op.create_index(
        "ix_payment_events_merchant_ts", "payment_events", ["merchant_id", "event_timestamp"]
    )


def downgrade() -> None:
    op.drop_index("ix_payment_events_merchant_ts", table_name="payment_events")
    op.drop_index("ix_payment_events_transaction_ts", table_name="payment_events")
    op.drop_table("payment_events")

    op.drop_index("ix_transactions_discrepant", table_name="transactions")
    op.drop_index("ix_transactions_failed_settled", table_name="transactions")
    op.drop_index("ix_transactions_processed_unsettled", table_name="transactions")
    op.drop_index("ix_transactions_merchant_first_event_at", table_name="transactions")
    op.drop_index("ix_transactions_merchant_status", table_name="transactions")
    op.drop_table("transactions")

    op.drop_table("merchants")

    bind = op.get_bind()
    event_type_enum.drop(bind, checkfirst=True)
    settlement_status_enum.drop(bind, checkfirst=True)
    payment_status_enum.drop(bind, checkfirst=True)

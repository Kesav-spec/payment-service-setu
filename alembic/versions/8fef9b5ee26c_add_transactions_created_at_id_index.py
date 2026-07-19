"""add transactions (created_at, id) index for cursor pagination

Revision ID: 8fef9b5ee26c
Revises: d74559a56dee
Create Date: 2026-07-19
"""

from alembic import op

revision = "8fef9b5ee26c"
down_revision = "d74559a56dee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_transactions_created_at_id", "transactions", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_created_at_id", table_name="transactions")

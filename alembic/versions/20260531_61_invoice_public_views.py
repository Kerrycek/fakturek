"""track public invoice views

Revision ID: 20260531_61
Revises: 20260530_60
Create Date: 2026-05-31 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260531_61"
down_revision = "20260525_59"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("public_view_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "invoices",
        sa.Column("public_first_viewed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("public_last_viewed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoices", "public_last_viewed_at")
    op.drop_column("invoices", "public_first_viewed_at")
    op.drop_column("invoices", "public_view_count")

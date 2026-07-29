"""add invoice item units

Revision ID: 20260316_49
Revises: 20260316_48
Create Date: 2026-03-16 23:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_49"
down_revision = "20260316_48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_items",
        sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "invoice_catalog_items",
        sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
    )

    op.alter_column("invoice_items", "unit", server_default=None)
    op.alter_column("invoice_catalog_items", "unit", server_default=None)


def downgrade() -> None:
    op.drop_column("invoice_catalog_items", "unit")
    op.drop_column("invoice_items", "unit")

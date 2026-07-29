"""add invoice style selectors

Revision ID: 20260531_62
Revises: 20260531_61
Create Date: 2026-05-31 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260531_62"
down_revision = "20260531_61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column("default_invoice_style", sa.String(length=32), nullable=False, server_default="modern"),
    )
    op.add_column(
        "invoices",
        sa.Column("invoice_style", sa.String(length=32), nullable=False, server_default="modern"),
    )


def downgrade() -> None:
    op.drop_column("invoices", "invoice_style")
    op.drop_column("subjects", "default_invoice_style")

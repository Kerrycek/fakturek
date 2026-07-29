"""add invoice language

Revision ID: 20260421_55
Revises: 20260326_54
Create Date: 2026-04-21 18:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260421_55"
down_revision = "20260326_54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("invoice_language", sa.String(length=5), nullable=False, server_default="cs"),
    )
    op.alter_column("invoices", "invoice_language", server_default=None)


def downgrade() -> None:
    op.drop_column("invoices", "invoice_language")

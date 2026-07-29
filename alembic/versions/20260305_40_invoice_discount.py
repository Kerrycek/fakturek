"""Invoice discount / subtotal support.

Revision ID: 20260305_40
Revises: 20260302_32
Create Date: 2026-03-05

Phase-40 introduces invoice-level discount support. The subtotal itself is still
computed from invoice items, but we persist the invoice discount so the final
amount is reproducible in detail, print and PDF views.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260305_40"
down_revision = "20260302_32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "discount_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    try:
        op.drop_column("invoices", "discount_cents")
    except Exception:
        pass

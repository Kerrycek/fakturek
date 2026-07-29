"""Invoice payment method and footer text.

Revision ID: 20260314_42
Revises: 20260305_41
Create Date: 2026-03-14

Phase-42 adds invoice-level payment method and footer text controls so each
invoice can render an appropriate payment block and legal footer in preview/PDF.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260314_42"
down_revision = "20260305_41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "payment_method",
            sa.String(length=32),
            nullable=False,
            server_default="bank_transfer",
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "footer_mode",
            sa.String(length=32),
            nullable=False,
            server_default="trade_register",
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "footer_text",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    for column_name in ["footer_text", "footer_mode", "payment_method"]:
        try:
            op.drop_column("invoices", column_name)
        except Exception:
            pass

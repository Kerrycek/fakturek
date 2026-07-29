"""Contact fixed VS and subject invoice footer defaults.

Revision ID: 20260314_43
Revises: 20260314_42
Create Date: 2026-03-14

Adds:
- contacts.fixed_variable_symbol
- invoices.variable_symbol
- subjects.default_invoice_footer_mode
- subjects.default_invoice_footer_text
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260314_43"
down_revision = "20260314_42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "fixed_variable_symbol",
            sa.String(length=10),
            nullable=True,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "variable_symbol",
            sa.String(length=10),
            nullable=True,
        ),
    )
    op.add_column(
        "subjects",
        sa.Column(
            "default_invoice_footer_mode",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "subjects",
        sa.Column(
            "default_invoice_footer_text",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    for table_name, column_name in [
        ("subjects", "default_invoice_footer_text"),
        ("subjects", "default_invoice_footer_mode"),
        ("invoices", "variable_symbol"),
        ("contacts", "fixed_variable_symbol"),
    ]:
        try:
            op.drop_column(table_name, column_name)
        except Exception:
            pass

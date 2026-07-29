"""Add subject invoice PDF theme.

Revision ID: 20260607_71
Revises: 20260607_70
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260607_71"
down_revision = "20260607_70"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "invoice_pdf_theme" not in _cols("subjects"):
        op.add_column("subjects", sa.Column("invoice_pdf_theme", sa.String(length=32), nullable=False, server_default="standard"))
    op.get_bind().execute(sa.text("UPDATE subjects SET invoice_pdf_theme = 'standard' WHERE invoice_pdf_theme IS NULL OR invoice_pdf_theme = ''"))


def downgrade() -> None:
    if "invoice_pdf_theme" in _cols("subjects"):
        op.drop_column("subjects", "invoice_pdf_theme")

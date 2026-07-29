"""add invoice document types

Revision ID: 20260316_47
Revises: 20260316_46
Create Date: 2026-03-16 19:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_47"
down_revision = "20260316_46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("document_type", sa.String(length=20), nullable=False, server_default="invoice"),
    )
    op.add_column(
        "invoices",
        sa.Column("source_invoice_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_invoices_subject_type", "invoices", ["subject_id", "document_type"], unique=False)
    op.create_index("ix_invoices_source_invoice_id", "invoices", ["source_invoice_id"], unique=False)
    op.create_foreign_key(
        "fk_invoices_source_invoice_id",
        "invoices",
        "invoices",
        ["source_invoice_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE invoices SET document_type = 'invoice' WHERE document_type IS NULL OR document_type = ''")
    op.alter_column("invoices", "document_type", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_invoices_source_invoice_id", "invoices", type_="foreignkey")
    op.drop_index("ix_invoices_source_invoice_id", table_name="invoices")
    op.drop_index("ix_invoices_subject_type", table_name="invoices")
    op.drop_column("invoices", "source_invoice_id")
    op.drop_column("invoices", "document_type")

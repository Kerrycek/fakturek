"""Invoice catalog / favourite items.

Revision ID: 20260305_41
Revises: 20260305_40
Create Date: 2026-03-05

Phase-41 introduces a simple per-subject catalog of favourite invoice items.
Users can save reusable rows directly from the invoice editor and insert them
later without waiting for invoice history suggestions to exist.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260305_41"
down_revision = "20260305_40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_catalog_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False, server_default="1.00"),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vat_rate", sa.Numeric(5, 2), nullable=False, server_default="0.00"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CZK"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_invoice_catalog_items_subject_id",
        "invoice_catalog_items",
        ["subject_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_catalog_items_subject_currency",
        "invoice_catalog_items",
        ["subject_id", "currency"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_catalog_items_subject_description",
        "invoice_catalog_items",
        ["subject_id", "description"],
        unique=False,
    )


def downgrade() -> None:
    for index_name in [
        "ix_invoice_catalog_items_subject_description",
        "ix_invoice_catalog_items_subject_currency",
        "ix_invoice_catalog_items_subject_id",
    ]:
        try:
            op.drop_index(index_name, table_name="invoice_catalog_items")
        except Exception:
            pass
    try:
        op.drop_table("invoice_catalog_items")
    except Exception:
        pass

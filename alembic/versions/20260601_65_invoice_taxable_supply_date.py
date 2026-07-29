"""add invoice taxable supply date

Revision ID: 20260601_65
Revises: 20260601_64
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260601_65"
down_revision = "20260601_64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("taxable_supply_date", sa.Date(), nullable=True))
    op.execute("UPDATE invoices SET taxable_supply_date = issue_date WHERE taxable_supply_date IS NULL")
    op.create_index("ix_invoices_subject_taxable_supply_date", "invoices", ["subject_id", "taxable_supply_date"])


def downgrade() -> None:
    op.drop_index("ix_invoices_subject_taxable_supply_date", table_name="invoices")
    op.drop_column("invoices", "taxable_supply_date")

"""Add currency to subject bank accounts.

Revision ID: 20260315_44
Revises: 20260314_43
Create Date: 2026-03-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260315_44"
down_revision = "20260314_43"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subject_bank_accounts") as batch_op:
        batch_op.add_column(sa.Column("currency", sa.String(length=3), nullable=False, server_default="CZK"))

    op.execute("UPDATE subject_bank_accounts SET currency = 'CZK' WHERE currency IS NULL OR currency = ''")

    with op.batch_alter_table("subject_bank_accounts") as batch_op:
        batch_op.alter_column("currency", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("subject_bank_accounts") as batch_op:
        batch_op.drop_column("currency")

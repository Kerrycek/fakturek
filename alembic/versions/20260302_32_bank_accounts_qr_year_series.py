"""Bank accounts, invoice account snapshots, and year-aware numbering.

Revision ID: 20260302_32
Revises: 20260222_24
Create Date: 2026-03-02

This migration adds:
- subject_bank_accounts table for managing multiple bank accounts per subject
- invoice-level bank account snapshot columns so historical invoices stay stable
- invoice_series.last_counter_year for year-prefixed numbering resets
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20260302_32"
down_revision = "20260222_24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subject_bank_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("account_number", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("bic", sa.String(length=11), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=False, server_default="CZ"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_subject_bank_accounts_subject_id", "subject_bank_accounts", ["subject_id"], unique=False)
    op.create_index(
        "ix_subject_bank_accounts_subject_default",
        "subject_bank_accounts",
        ["subject_id", "is_default"],
        unique=False,
    )

    op.add_column("invoice_series", sa.Column("last_counter_year", sa.Integer(), nullable=True))
    try:
        op.execute(
            sa.text(
                "UPDATE invoice_series SET last_counter_year = :year WHERE last_counter_year IS NULL"
            ).bindparams(year=int(datetime.utcnow().year))
        )
    except Exception:
        pass

    op.add_column("invoices", sa.Column("bank_account_id", sa.BigInteger(), nullable=True))
    op.add_column("invoices", sa.Column("bank_account_label", sa.String(length=120), nullable=True))
    op.add_column("invoices", sa.Column("bank_account_number", sa.String(length=255), nullable=True))
    op.add_column("invoices", sa.Column("bank_account_iban", sa.String(length=34), nullable=True))
    op.add_column("invoices", sa.Column("bank_account_bic", sa.String(length=11), nullable=True))
    op.add_column("invoices", sa.Column("bank_account_country", sa.String(length=2), nullable=True))

    op.create_index("ix_invoices_bank_account_id", "invoices", ["bank_account_id"], unique=False)
    op.create_foreign_key(
        "fk_invoices_bank_account_id",
        "invoices",
        "subject_bank_accounts",
        ["bank_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    try:
        op.drop_constraint("fk_invoices_bank_account_id", "invoices", type_="foreignkey")
    except Exception:
        pass
    try:
        op.drop_index("ix_invoices_bank_account_id", table_name="invoices")
    except Exception:
        pass

    for column_name in [
        "bank_account_country",
        "bank_account_bic",
        "bank_account_iban",
        "bank_account_number",
        "bank_account_label",
        "bank_account_id",
    ]:
        try:
            op.drop_column("invoices", column_name)
        except Exception:
            pass

    try:
        op.drop_column("invoice_series", "last_counter_year")
    except Exception:
        pass

    for index_name in [
        "ix_subject_bank_accounts_subject_default",
        "ix_subject_bank_accounts_subject_id",
    ]:
        try:
            op.drop_index(index_name, table_name="subject_bank_accounts")
        except Exception:
            pass
    try:
        op.drop_table("subject_bank_accounts")
    except Exception:
        pass

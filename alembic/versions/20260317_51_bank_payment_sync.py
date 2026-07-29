"""add bank payment sync tables

Revision ID: 20260317_51
Revises: 20260317_50
Create Date: 2026-03-17 16:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_51"
down_revision = "20260317_50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_provider", sa.String(length=32), nullable=False, server_default="none"),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_auto_pair", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("fio_api_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_last_checked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_last_success_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_cursor_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_last_error", sa.Text(), nullable=True),
    )

    op.alter_column("subject_bank_accounts", "payment_sync_provider", server_default=None)
    op.alter_column("subject_bank_accounts", "payment_sync_enabled", server_default=None)
    op.alter_column("subject_bank_accounts", "payment_sync_auto_pair", server_default=None)

    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_bank_account_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="fio_api"),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("booked_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CZK"),
        sa.Column("direction", sa.String(length=16), nullable=False, server_default="incoming"),
        sa.Column("variable_symbol", sa.String(length=10), nullable=True),
        sa.Column("constant_symbol", sa.String(length=4), nullable=True),
        sa.Column("specific_symbol", sa.String(length=10), nullable=True),
        sa.Column("counterparty_account", sa.String(length=255), nullable=True),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.Column("matched_invoice_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["matched_invoice_id"], ["invoices.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.ForeignKeyConstraint(["subject_bank_account_id"], ["subject_bank_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_bank_account_id",
            "provider",
            "external_id",
            name="uq_bank_transactions_account_provider_external",
        ),
    )
    op.create_index("ix_bank_transactions_subject_bank_account_id", "bank_transactions", ["subject_bank_account_id"], unique=False)
    op.create_index("ix_bank_transactions_booked_on", "bank_transactions", ["booked_on"], unique=False)
    op.create_index("ix_bank_transactions_matched_invoice_id", "bank_transactions", ["matched_invoice_id"], unique=False)
    op.create_index("ix_bank_transactions_payment_id", "bank_transactions", ["payment_id"], unique=False)
    op.create_index(
        "ix_bank_transactions_account_booked_on",
        "bank_transactions",
        ["subject_bank_account_id", "booked_on"],
        unique=False,
    )

    op.alter_column("bank_transactions", "provider", server_default=None)
    op.alter_column("bank_transactions", "amount_cents", server_default=None)
    op.alter_column("bank_transactions", "currency", server_default=None)
    op.alter_column("bank_transactions", "direction", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_bank_transactions_account_booked_on", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_payment_id", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_matched_invoice_id", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_booked_on", table_name="bank_transactions")
    op.drop_index("ix_bank_transactions_subject_bank_account_id", table_name="bank_transactions")
    op.drop_table("bank_transactions")

    op.drop_column("subject_bank_accounts", "payment_sync_last_error")
    op.drop_column("subject_bank_accounts", "payment_sync_cursor_date")
    op.drop_column("subject_bank_accounts", "payment_sync_last_success_at")
    op.drop_column("subject_bank_accounts", "payment_sync_last_checked_at")
    op.drop_column("subject_bank_accounts", "fio_api_token")
    op.drop_column("subject_bank_accounts", "payment_sync_auto_pair")
    op.drop_column("subject_bank_accounts", "payment_sync_enabled")
    op.drop_column("subject_bank_accounts", "payment_sync_provider")

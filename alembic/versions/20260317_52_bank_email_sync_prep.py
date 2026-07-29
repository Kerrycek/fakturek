"""add bank email sync preparation

Revision ID: 20260317_52
Revises: 20260317_51
Create Date: 2026-03-17 22:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_52"
down_revision = "20260317_51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_email_sender_filter", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_email_subject_filter", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_email_parser", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_last_email_uid", sa.String(length=64), nullable=True),
    )
    op.alter_column("subject_bank_accounts", "payment_sync_email_parser", server_default=None)

    op.create_table(
        "bank_incoming_emails",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_bank_account_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="email_bank"),
        sa.Column("imap_uid", sa.String(length=64), nullable=True),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("from_email", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("raw_headers_json", sa.Text(), nullable=True),
        sa.Column("processing_status", sa.String(length=32), nullable=False, server_default="stored"),
        sa.Column("processing_note", sa.Text(), nullable=True),
        sa.Column("matched_bank_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["matched_bank_transaction_id"], ["bank_transactions.id"]),
        sa.ForeignKeyConstraint(["subject_bank_account_id"], ["subject_bank_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_bank_account_id",
            "provider",
            "imap_uid",
            name="uq_bank_incoming_emails_account_provider_uid",
        ),
    )
    op.create_index("ix_bank_incoming_emails_subject_bank_account_id", "bank_incoming_emails", ["subject_bank_account_id"], unique=False)
    op.create_index("ix_bank_incoming_emails_received_at", "bank_incoming_emails", ["received_at"], unique=False)
    op.create_index("ix_bank_incoming_emails_matched_bank_transaction_id", "bank_incoming_emails", ["matched_bank_transaction_id"], unique=False)
    op.create_index(
        "ix_bank_incoming_emails_account_received_at",
        "bank_incoming_emails",
        ["subject_bank_account_id", "received_at"],
        unique=False,
    )
    op.alter_column("bank_incoming_emails", "provider", server_default=None)
    op.alter_column("bank_incoming_emails", "processing_status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_bank_incoming_emails_account_received_at", table_name="bank_incoming_emails")
    op.drop_index("ix_bank_incoming_emails_matched_bank_transaction_id", table_name="bank_incoming_emails")
    op.drop_index("ix_bank_incoming_emails_received_at", table_name="bank_incoming_emails")
    op.drop_index("ix_bank_incoming_emails_subject_bank_account_id", table_name="bank_incoming_emails")
    op.drop_table("bank_incoming_emails")

    op.drop_column("subject_bank_accounts", "payment_sync_last_email_uid")
    op.drop_column("subject_bank_accounts", "payment_sync_email_parser")
    op.drop_column("subject_bank_accounts", "payment_sync_email_subject_filter")
    op.drop_column("subject_bank_accounts", "payment_sync_email_sender_filter")

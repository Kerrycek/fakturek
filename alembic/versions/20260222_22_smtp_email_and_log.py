"""SMTP invoice email sending log fields.

Revision ID: 20260222_22
Revises: 20260218_18
Create Date: 2026-02-22

Phase-22 adds basic SMTP email sending and improves the `invoice_emails` log.

DB changes:

- Add `from_email` (non-null, default '')
- Add `message_id` (optional)
- Add `error_message` (optional)
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260222_22"
down_revision = "20260218_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_emails",
        sa.Column(
            "from_email",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "invoice_emails",
        sa.Column(
            "message_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "invoice_emails",
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("invoice_emails", "error_message")
    op.drop_column("invoice_emails", "message_id")
    op.drop_column("invoice_emails", "from_email")

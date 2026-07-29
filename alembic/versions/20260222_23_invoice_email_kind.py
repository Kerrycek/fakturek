"""Invoice email kind.

Revision ID: 20260222_23
Revises: 20260222_22
Create Date: 2026-02-22

Phase-23 introduces payment reminders. We want to distinguish between
"invoice" emails and "reminder" emails in the audit log.

DB changes:

- Add `invoice_emails.kind` (non-null, default 'invoice')

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260222_23"
down_revision = "20260222_22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_emails",
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
            server_default="invoice",
        ),
    )

    # Helpful for filtering/reporting.
    try:
        op.create_index("ix_invoice_emails_kind", "invoice_emails", ["kind"], unique=False)
    except Exception:
        # Some backends or older DBs might already have the index via ORM.
        pass


def downgrade() -> None:
    try:
        op.drop_index("ix_invoice_emails_kind", table_name="invoice_emails")
    except Exception:
        pass
    op.drop_column("invoice_emails", "kind")

"""add bank alert email aliases

Revision ID: 20260325_53
Revises: 20260317_52
Create Date: 2026-03-25 22:30:00.000000
"""

from __future__ import annotations

import secrets
import string

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260325_53"
down_revision = "20260317_52"
branch_labels = None
depends_on = None


_ALPHABET = "".join(ch for ch in (string.ascii_letters + string.digits) if ch not in {"0", "O", "I", "l"})


def _generate_localpart(existing: set[str]) -> str:
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def upgrade() -> None:
    op.add_column(
        "subject_bank_accounts",
        sa.Column("payment_sync_alert_localpart", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_subject_bank_accounts_payment_sync_alert_localpart",
        "subject_bank_accounts",
        ["payment_sync_alert_localpart"],
        unique=True,
    )

    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, payment_sync_alert_localpart FROM subject_bank_accounts ORDER BY id ASC"
            )
        )
    )
    existing = {
        str(row.payment_sync_alert_localpart).strip()
        for row in rows
        if getattr(row, "payment_sync_alert_localpart", None)
    }
    for row in rows:
        current = str(getattr(row, "payment_sync_alert_localpart", "") or "").strip()
        if current:
            continue
        bind.execute(
            sa.text(
                "UPDATE subject_bank_accounts SET payment_sync_alert_localpart = :value WHERE id = :id"
            ),
            {"value": _generate_localpart(existing), "id": int(row.id)},
        )


def downgrade() -> None:
    op.drop_index("ix_subject_bank_accounts_payment_sync_alert_localpart", table_name="subject_bank_accounts")
    op.drop_column("subject_bank_accounts", "payment_sync_alert_localpart")

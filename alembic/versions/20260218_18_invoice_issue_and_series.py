"""Invoice issue + numbering series seed.

Revision ID: 20260218_18
Revises: 20260217_11
Create Date: 2026-02-18

Phase-18 introduces transactional invoice numbering via ``invoice_series`` and
makes issued invoices immutable.

DB changes:

- Ensure each subject has a default invoice series (name='default').
- Enforce unique invoice numbers per subject.
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20260218_18"
down_revision = "20260217_11"
branch_labels = None
depends_on = None


def _utcnow() -> datetime:
    return datetime.utcnow()


def upgrade() -> None:
    # Seed a default invoice series for every existing subject.
    now = _utcnow()

    # MySQL/MariaDB: INSERT .. SELECT is the most convenient way to do this in one go.
    # For safety, we only insert for subjects that do not yet have (subject_id, name='default').
    op.execute(
        sa.text(
            """
            INSERT INTO invoice_series (subject_id, name, prefix, pad_length, last_counter, created_at, updated_at)
            SELECT s.id, 'default', '', 4, 0, :now, :now
            FROM subjects s
            LEFT JOIN invoice_series i
              ON i.subject_id = s.id AND i.name = 'default'
            WHERE i.id IS NULL
            """
        ).bindparams(now=now)
    )

    # Enforce unique invoice numbers per subject.
    op.create_unique_constraint(
        "uq_invoices_subject_number",
        "invoices",
        ["subject_id", "number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_invoices_subject_number", "invoices", type_="unique")

    # We intentionally do NOT delete seeded invoice_series rows on downgrade.
    # Downgrades should not destroy user data.

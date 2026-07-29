"""Add VAT identified person flag to subjects.

Revision ID: 20260603_66
Revises: 20260601_65
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260603_66"
down_revision = "20260601_65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column(
            "is_vat_identified_person",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "subjects",
        "is_vat_identified_person",
        existing_type=sa.Boolean(),
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("subjects", "is_vat_identified_person")

"""Add subject legal form for tax UI relevance.

Revision ID: 20260603_69
Revises: 20260603_68
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260603_69"
down_revision = "20260603_67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column("legal_form", sa.String(length=24), nullable=False, server_default="business"),
    )


def downgrade() -> None:
    op.drop_column("subjects", "legal_form")

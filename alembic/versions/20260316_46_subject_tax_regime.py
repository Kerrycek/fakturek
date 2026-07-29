"""add subject tax regime settings

Revision ID: 20260316_46
Revises: 20260316_45
Create Date: 2026-03-16 18:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_46"
down_revision = "20260316_45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column("tax_regime", sa.String(length=16), nullable=False, server_default="standard"),
    )
    op.add_column(
        "subjects",
        sa.Column("flat_tax_band", sa.String(length=8), nullable=False, server_default="1"),
    )
    op.add_column(
        "subjects",
        sa.Column("flat_tax_income_profile", sa.String(length=24), nullable=False, server_default="general"),
    )
    op.execute("UPDATE subjects SET tax_regime = 'standard' WHERE tax_regime IS NULL OR tax_regime = ''")
    op.execute("UPDATE subjects SET flat_tax_band = '1' WHERE flat_tax_band IS NULL OR flat_tax_band = ''")
    op.execute(
        "UPDATE subjects SET flat_tax_income_profile = 'general' "
        "WHERE flat_tax_income_profile IS NULL OR flat_tax_income_profile = ''"
    )
    op.alter_column("subjects", "tax_regime", server_default=None)
    op.alter_column("subjects", "flat_tax_band", server_default=None)
    op.alter_column("subjects", "flat_tax_income_profile", server_default=None)


def downgrade() -> None:
    op.drop_column("subjects", "flat_tax_income_profile")
    op.drop_column("subjects", "flat_tax_band")
    op.drop_column("subjects", "tax_regime")

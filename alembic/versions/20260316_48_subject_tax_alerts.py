"""add subject tax alert settings

Revision ID: 20260316_48
Revises: 20260316_47
Create Date: 2026-03-16 21:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_48"
down_revision = "20260316_47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column("tax_alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "subjects",
        sa.Column("tax_alert_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "subjects",
        sa.Column("vat_alert_last_stage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subjects",
        sa.Column("vat_alert_last_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subjects",
        sa.Column("flat_tax_alert_last_stage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subjects",
        sa.Column("flat_tax_alert_last_year", sa.Integer(), nullable=True),
    )

    op.alter_column("subjects", "tax_alerts_enabled", server_default=None)
    op.alter_column("subjects", "vat_alert_last_stage", server_default=None)
    op.alter_column("subjects", "flat_tax_alert_last_stage", server_default=None)


def downgrade() -> None:
    op.drop_column("subjects", "flat_tax_alert_last_year")
    op.drop_column("subjects", "flat_tax_alert_last_stage")
    op.drop_column("subjects", "vat_alert_last_year")
    op.drop_column("subjects", "vat_alert_last_stage")
    op.drop_column("subjects", "tax_alert_email")
    op.drop_column("subjects", "tax_alerts_enabled")

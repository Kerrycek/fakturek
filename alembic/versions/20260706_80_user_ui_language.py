"""add user interface language

Revision ID: 20260706_80
Revises: 20260701_79
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260706_80"
down_revision = "20260619_73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ui_language", sa.String(length=8), nullable=False, server_default="cs"),
    )
    op.execute("UPDATE users SET ui_language = 'cs' WHERE ui_language IS NULL OR ui_language = ''")
    op.alter_column("users", "ui_language", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "ui_language")

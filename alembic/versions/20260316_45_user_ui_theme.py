"""add user ui theme

Revision ID: 20260316_45
Revises: 20260315_44
Create Date: 2026-03-16 17:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_45"
down_revision = "20260315_44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ui_theme", sa.String(length=16), nullable=False, server_default="light"),
    )
    op.execute("UPDATE users SET ui_theme = 'light' WHERE ui_theme IS NULL OR ui_theme = ''")
    op.alter_column("users", "ui_theme", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "ui_theme")

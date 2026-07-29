"""add user email verification timestamp

Revision ID: 20260525_59
Revises: 20260425_58
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260525_59"
down_revision = "20260423_57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE users
               SET email_verified_at = COALESCE(last_login_at, created_at, updated_at)
             WHERE is_active = :active
               AND email_verified_at IS NULL
            """
        ),
        {"active": True},
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")

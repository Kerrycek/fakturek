"""Add user account deletion scheduling fields.

Revision ID: 20260603_67
Revises: 20260603_66
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260603_67"
down_revision = "20260603_66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deletion_requested_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("deletion_scheduled_for", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("deletion_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deletion_reason")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "deletion_scheduled_for")
    op.drop_column("users", "deletion_requested_at")

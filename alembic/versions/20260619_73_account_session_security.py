"""Add account session security controls.

Revision ID: 20260619_73
Revises: 20260610_72
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260619_73"
down_revision = "20260610_72"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    cols = _cols("users")
    if "session_version" not in cols:
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))
    if "session_max_age_days" not in cols:
        op.add_column("users", sa.Column("session_max_age_days", sa.Integer(), nullable=False, server_default="7"))
    if "failed_login_count" not in cols:
        op.add_column("users", sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"))
    if "failed_login_locked_until" not in cols:
        op.add_column("users", sa.Column("failed_login_locked_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    cols = _cols("users")
    if "failed_login_locked_until" in cols:
        op.drop_column("users", "failed_login_locked_until")
    if "failed_login_count" in cols:
        op.drop_column("users", "failed_login_count")
    if "session_max_age_days" in cols:
        op.drop_column("users", "session_max_age_days")
    if "session_version" in cols:
        op.drop_column("users", "session_version")

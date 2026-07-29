"""Add API token sandbox mode.

Revision ID: 20260610_72
Revises: 20260607_71
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260610_72"
down_revision = "20260607_71"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "is_sandbox" not in _cols("api_tokens"):
        op.add_column("api_tokens", sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default=sa.false()))
    falsity = "0" if op.get_bind().dialect.name == "sqlite" else "FALSE"
    op.get_bind().execute(sa.text(f"UPDATE api_tokens SET is_sandbox = {falsity} WHERE is_sandbox IS NULL"))


def downgrade() -> None:
    if "is_sandbox" in _cols("api_tokens"):
        op.drop_column("api_tokens", "is_sandbox")

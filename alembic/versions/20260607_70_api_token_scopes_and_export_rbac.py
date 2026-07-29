"""Add API token scopes and export RBAC.

Revision ID: 20260607_70
Revises: 20260603_69
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260607_70"
down_revision = "20260603_69"
branch_labels = None
depends_on = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    user_subject_cols = _cols("user_subjects")
    if "can_export" not in user_subject_cols:
        op.add_column("user_subjects", sa.Column("can_export", sa.Boolean(), nullable=False, server_default=sa.true()))
    api_token_cols = _cols("api_tokens")
    for name in ("can_read", "can_write", "can_issue", "can_export"):
        if name not in api_token_cols:
            op.add_column("api_tokens", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))

    truth = "1" if dialect == "sqlite" else "TRUE"
    falsity = "0" if dialect == "sqlite" else "FALSE"
    bind.execute(sa.text(f"UPDATE user_subjects SET can_export = {truth} WHERE role IN ('owner','manager','accountant') OR can_edit = {truth} OR can_issue = {truth}"))
    bind.execute(sa.text(f"UPDATE user_subjects SET can_export = {falsity} WHERE role = 'viewer' AND can_edit = {falsity} AND can_issue = {falsity}"))
    bind.execute(sa.text(f"UPDATE api_tokens SET can_read = COALESCE(can_read, {truth}), can_write = COALESCE(can_write, {truth}), can_issue = COALESCE(can_issue, {truth}), can_export = COALESCE(can_export, {truth})"))


def downgrade() -> None:
    api_token_cols = _cols("api_tokens")
    for name in ("can_export", "can_issue", "can_write", "can_read"):
        if name in api_token_cols:
            op.drop_column("api_tokens", name)
    if "can_export" in _cols("user_subjects"):
        op.drop_column("user_subjects", "can_export")

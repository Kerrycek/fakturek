"""add api token monthly quota tracking

Revision ID: 20260531_63
Revises: 20260531_62
Create Date: 2026-05-31 23:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260531_63"
down_revision = "20260531_62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "api_token_monthly_usage" in tables:
        return

    op.create_table(
        "api_token_monthly_usage",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("api_tokens.id"),
            nullable=False,
        ),
        sa.Column("usage_year", sa.Integer(), nullable=False),
        sa.Column("usage_month", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("last_request_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("token_id", "usage_year", "usage_month", name="uq_api_token_monthly_usage_scope"),
    )
    op.create_index(
        "ix_api_token_monthly_usage_lookup",
        "api_token_monthly_usage",
        ["token_id", "usage_year", "usage_month"],
        unique=False,
    )
    op.create_index(
        "ix_api_token_monthly_usage_token_id",
        "api_token_monthly_usage",
        ["token_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "api_token_monthly_usage" not in tables:
        return
    indexes = {index["name"] for index in inspector.get_indexes("api_token_monthly_usage")}
    if "ix_api_token_monthly_usage_lookup" in indexes:
        op.drop_index("ix_api_token_monthly_usage_lookup", table_name="api_token_monthly_usage")
    if "ix_api_token_monthly_usage_token_id" in indexes:
        op.drop_index("ix_api_token_monthly_usage_token_id", table_name="api_token_monthly_usage")
    op.drop_table("api_token_monthly_usage")

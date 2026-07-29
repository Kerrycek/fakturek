"""add api v1 tokens and idempotency tables

Revision ID: 20260326_54
Revises: 20260325_53
Create Date: 2026-03-26 19:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260326_54"
down_revision = "20260325_53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("token_prefix", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"], unique=False)
    op.create_index(
        "ix_api_tokens_user_revoked",
        "api_tokens",
        ["user_id", "revoked_at"],
        unique=False,
    )

    op.alter_column("api_tokens", "name", server_default=None)
    op.alter_column("api_tokens", "token_prefix", server_default=None)

    op.create_table(
        "api_idempotency_keys",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("subject_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("request_method", sa.String(length=12), nullable=False),
        sa.Column("request_path", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "request_method",
            "request_path",
            "idempotency_key",
            name="uq_api_idempotency_scope",
        ),
    )
    op.create_index("ix_api_idempotency_keys_user_id", "api_idempotency_keys", ["user_id"], unique=False)
    op.create_index("ix_api_idempotency_keys_subject_id", "api_idempotency_keys", ["subject_id"], unique=False)
    op.create_index(
        "ix_api_idempotency_lookup",
        "api_idempotency_keys",
        ["user_id", "request_method", "request_path", "idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_api_idempotency_lookup", table_name="api_idempotency_keys")
    op.drop_index("ix_api_idempotency_keys_subject_id", table_name="api_idempotency_keys")
    op.drop_index("ix_api_idempotency_keys_user_id", table_name="api_idempotency_keys")
    op.drop_table("api_idempotency_keys")

    op.drop_index("ix_api_tokens_user_revoked", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")

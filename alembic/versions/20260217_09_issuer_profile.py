"""Issuer profile (seller settings) stored in DB.

Revision ID: 20260217_09
Revises: 20260215_02
Create Date: 2026-02-17

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260217_09"
down_revision = "20260215_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issuer_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("street", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("city", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("zip", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=2), nullable=False, server_default="CZ"),
        sa.Column("ico", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("dic", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("bank_account", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("issuer_profiles")

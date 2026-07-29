"""Add contact registry sync metadata.

Revision ID: 20260601_64
Revises: 20260531_63
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260601_64"
down_revision = "20260531_63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("registry_auto_update", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("contacts", sa.Column("registry_last_checked_at", sa.DateTime(), nullable=True))
    op.add_column("contacts", sa.Column("registry_last_changed_at", sa.DateTime(), nullable=True))
    op.add_column("contacts", sa.Column("registry_last_source", sa.String(length=32), nullable=True))
    op.add_column("contacts", sa.Column("registry_last_error", sa.Text(), nullable=True))
    op.add_column("contacts", sa.Column("registry_data_hash", sa.String(length=64), nullable=True))
    op.add_column("contacts", sa.Column("registry_update_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_contacts_registry_auto", "contacts", ["subject_id", "registry_auto_update", "country", "ico"])
    op.alter_column("contacts", "registry_auto_update", server_default=None)
    op.alter_column("contacts", "registry_update_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_contacts_registry_auto", table_name="contacts")
    op.drop_column("contacts", "registry_update_count")
    op.drop_column("contacts", "registry_data_hash")
    op.drop_column("contacts", "registry_last_error")
    op.drop_column("contacts", "registry_last_source")
    op.drop_column("contacts", "registry_last_changed_at")
    op.drop_column("contacts", "registry_last_checked_at")
    op.drop_column("contacts", "registry_auto_update")

"""add catalog CSV upload dedupe key

Revision ID: 20260919_82
Revises: 20260706_80
Create Date: 2026-09-19
"""
# ruff: noqa: I001

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260919_82"
down_revision = "20260706_80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL preserves all existing import semantics: only catalog CSV uploads
    # set this key, and SQL unique indexes permit multiple NULL values.
    op.add_column(
        "import_runs", sa.Column("upload_dedupe_key", sa.String(length=128), nullable=True)
    )
    op.create_index(
        "uq_import_run_sub_src_udk",
        "import_runs",
        ["subject_id", "source", "upload_dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_import_run_sub_src_udk", table_name="import_runs")
    op.drop_column("import_runs", "upload_dedupe_key")

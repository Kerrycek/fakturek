"""Import infra: uploaded file metadata.

Revision ID: 20260222_24
Revises: 20260222_23
Create Date: 2026-02-22

Phase-24 starts the Fakturoid import pipeline.

To support uploads (and later idempotent imports), we persist basic
metadata about the uploaded file on the import_runs row:

- original file name
- stored relative path under IMPORT_STORAGE_DIR
- sha256 hash and size
- mime type (best-effort)

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260222_24"
down_revision = "20260222_23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_runs",
        sa.Column(
            "file_name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "import_runs",
        sa.Column(
            "file_path",
            sa.String(length=1024),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "import_runs",
        sa.Column(
            "file_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "import_runs",
        sa.Column(
            "file_size_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "import_runs",
        sa.Column(
            "mime_type",
            sa.String(length=100),
            nullable=False,
            server_default="",
        ),
    )

    # Indexes to make duplicate detection / history browsing fast.
    try:
        op.create_index("ix_import_runs_file_sha256", "import_runs", ["file_sha256"], unique=False)
    except Exception:
        pass

    try:
        op.create_index(
            "ix_import_runs_subject_source_sha",
            "import_runs",
            ["subject_id", "source", "file_sha256"],
            unique=False,
        )
    except Exception:
        pass


def downgrade() -> None:
    for ix_name in [
        "ix_import_runs_subject_source_sha",
        "ix_import_runs_file_sha256",
    ]:
        try:
            op.drop_index(ix_name, table_name="import_runs")
        except Exception:
            pass

    for col in ["mime_type", "file_size_bytes", "file_sha256", "file_path", "file_name"]:
        try:
            op.drop_column("import_runs", col)
        except Exception:
            # Best-effort.
            pass

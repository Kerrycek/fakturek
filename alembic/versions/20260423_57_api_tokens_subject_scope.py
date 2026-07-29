"""scope api tokens to a single subject

Revision ID: 20260423_57
Revises: 20260423_56
Create Date: 2026-04-23 00:30:00.000000
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20260423_57"
down_revision = "20260421_55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("api_tokens")}
    indexes = {index["name"] for index in inspector.get_indexes("api_tokens")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("api_tokens")}

    if "subject_id" not in columns:
        op.add_column(
            "api_tokens",
            sa.Column(
                "subject_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            ),
        )
    if "ix_api_tokens_subject_id" not in indexes:
        op.create_index("ix_api_tokens_subject_id", "api_tokens", ["subject_id"], unique=False)
    if "ix_api_tokens_subject_revoked" not in indexes:
        op.create_index("ix_api_tokens_subject_revoked", "api_tokens", ["subject_id", "revoked_at"], unique=False)
    if "fk_api_tokens_subject_id_subjects" not in foreign_keys:
        op.create_foreign_key(
            "fk_api_tokens_subject_id_subjects",
            "api_tokens",
            "subjects",
            ["subject_id"],
            ["id"],
        )

    now = datetime.utcnow()

    scoped_users = bind.execute(
        sa.text(
            """
            SELECT us.user_id AS user_id,
                   MIN(us.subject_id) AS subject_id,
                   COUNT(DISTINCT us.subject_id) AS subject_count
            FROM user_subjects AS us
            WHERE us.can_view = 1
            GROUP BY us.user_id
            """
        )
    ).mappings()

    for row in scoped_users:
        if int(row["subject_count"] or 0) == 1:
            bind.execute(
                sa.text(
                    """
                    UPDATE api_tokens
                    SET subject_id = :subject_id,
                        updated_at = :updated_at
                    WHERE user_id = :user_id
                      AND subject_id IS NULL
                    """
                ),
                {
                    "subject_id": int(row["subject_id"]),
                    "updated_at": now,
                    "user_id": int(row["user_id"]),
                },
            )

    bind.execute(
        sa.text(
            """
            UPDATE api_tokens
            SET revoked_at = :revoked_at,
                updated_at = :updated_at
            WHERE subject_id IS NULL
              AND revoked_at IS NULL
            """
        ),
        {"revoked_at": now, "updated_at": now},
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("api_tokens")}
    indexes = {index["name"] for index in inspector.get_indexes("api_tokens")}
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("api_tokens")}

    if "ix_api_tokens_subject_revoked" in indexes:
        op.drop_index("ix_api_tokens_subject_revoked", table_name="api_tokens")
    if "ix_api_tokens_subject_id" in indexes:
        op.drop_index("ix_api_tokens_subject_id", table_name="api_tokens")
    if "fk_api_tokens_subject_id_subjects" in foreign_keys:
        op.drop_constraint("fk_api_tokens_subject_id_subjects", "api_tokens", type_="foreignkey")
    if "subject_id" in columns:
        op.drop_column("api_tokens", "subject_id")

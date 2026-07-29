"""add recurring invoice plans

Revision ID: 20260317_50
Revises: 20260316_49
Create Date: 2026-03-17 09:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_50"
down_revision = "20260316_49"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurring_invoice_plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("template_invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("interval_unit", sa.String(length=16), nullable=False, server_default="month"),
        sa.Column("interval_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_issue_date", sa.Date(), nullable=False),
        sa.Column("due_in_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_issue", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_send", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("email_override", sa.String(length=255), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_generated_invoice_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["last_generated_invoice_id"], ["invoices.id"]),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"]),
        sa.ForeignKeyConstraint(["template_invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recurring_invoice_plans_subject_id",
        "recurring_invoice_plans",
        ["subject_id"],
        unique=False,
    )
    op.create_index(
        "ix_recurring_invoice_plans_template_invoice_id",
        "recurring_invoice_plans",
        ["template_invoice_id"],
        unique=False,
    )
    op.create_index(
        "ix_recurring_invoice_plans_last_generated_invoice_id",
        "recurring_invoice_plans",
        ["last_generated_invoice_id"],
        unique=False,
    )
    op.create_index(
        "ix_recurring_plans_subject_active_date",
        "recurring_invoice_plans",
        ["subject_id", "is_active", "next_issue_date"],
        unique=False,
    )

    op.alter_column("recurring_invoice_plans", "interval_unit", server_default=None)
    op.alter_column("recurring_invoice_plans", "interval_count", server_default=None)
    op.alter_column("recurring_invoice_plans", "due_in_days", server_default=None)
    op.alter_column("recurring_invoice_plans", "is_active", server_default=None)
    op.alter_column("recurring_invoice_plans", "auto_issue", server_default=None)
    op.alter_column("recurring_invoice_plans", "auto_send", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_recurring_plans_subject_active_date", table_name="recurring_invoice_plans")
    op.drop_index("ix_recurring_invoice_plans_last_generated_invoice_id", table_name="recurring_invoice_plans")
    op.drop_index("ix_recurring_invoice_plans_template_invoice_id", table_name="recurring_invoice_plans")
    op.drop_index("ix_recurring_invoice_plans_subject_id", table_name="recurring_invoice_plans")
    op.drop_table("recurring_invoice_plans")

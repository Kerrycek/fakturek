"""Master plan schema expansion (subjects, RBAC, imports, audit).

Revision ID: 20260217_11
Revises: 20260217_09
Create Date: 2026-02-17

This migration adds the core tables required by the original project plan
(FÁZE 2 – DB modely + migrace) and extends existing MVP tables with
`subject_id` and other forward-compatible columns.

We keep legacy `issuer_profiles` for now as a bridge from early iterations.
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260217_11"
down_revision = "20260217_09"
branch_labels = None
depends_on = None


def _utcnow() -> datetime:
    # Alembic runs in Python; we store naive UTC datetimes.
    return datetime.utcnow()


def upgrade() -> None:
    # --- Identity / subjects / RBAC --------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "subjects",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_username", sa.String(length=64), nullable=True),
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
        sa.Column("is_vat_payer", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("default_currency", sa.String(length=3), nullable=False, server_default="CZK"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("public_username", name="uq_subjects_public_username"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "user_subjects",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="owner"),
        sa.Column("can_view", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("can_issue", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "subject_id", name="uq_user_subject"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_index("ix_user_subjects_user_id", "user_subjects", ["user_id"], unique=False)
    op.create_index("ix_user_subjects_subject_id", "user_subjects", ["subject_id"], unique=False)

    # Seed a single "default subject" for early MVP routes.
    # This keeps newly-added NOT NULL subject_id columns usable before the auth/subject switch is implemented.
    # Use a dialect-neutral existence check instead of MySQL-only ON DUPLICATE KEY UPDATE.
    now = _utcnow()
    bind = op.get_bind()
    existing_default_subject = bind.execute(sa.text("SELECT id FROM subjects WHERE id = 1 LIMIT 1")).scalar()
    if existing_default_subject is None:
        op.execute(
            sa.text(
                """
                INSERT INTO subjects (id, public_username, name, email, phone, street, city, zip, country,
                                     ico, dic, bank_account, is_vat_payer, default_currency, created_at, updated_at)
                VALUES (1, NULL, '', '', '', '', '', '', 'CZ', '', '', '', 0, 'CZK', :now, :now)
                """
            ).bindparams(now=now)
        )

    # --- Invoice series ---------------------------------------------------
    op.create_table(
        "invoice_series",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False, server_default="default"),
        sa.Column("prefix", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("pad_length", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("last_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("subject_id", "name", name="uq_invoice_series_subject_name"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_invoice_series_subject_id", "invoice_series", ["subject_id"], unique=False)

    # --- Extend existing MVP tables (contacts/invoices/items) -------------

    # contacts: subject_id + import metadata
    op.add_column(
        "contacts",
        sa.Column("subject_id", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_index("ix_contacts_subject_id", "contacts", ["subject_id"], unique=False)
    op.create_foreign_key(
        "fk_contacts_subject_id",
        "contacts",
        "subjects",
        ["subject_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.add_column("contacts", sa.Column("external_source", sa.String(length=32), nullable=True))
    op.add_column("contacts", sa.Column("external_id", sa.String(length=255), nullable=True))

    op.create_index("ix_contacts_subject_name", "contacts", ["subject_id", "name"], unique=False)
    op.create_index("ix_contacts_subject_email", "contacts", ["subject_id", "email"], unique=False)

    # invoices: subject_id + forward compatible fields
    op.add_column(
        "invoices",
        sa.Column("subject_id", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_index("ix_invoices_subject_id", "invoices", ["subject_id"], unique=False)
    op.create_foreign_key(
        "fk_invoices_subject_id",
        "invoices",
        "subjects",
        ["subject_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.add_column("invoices", sa.Column("internal_notes", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("buyer_name_cache", sa.String(length=255), nullable=True))
    op.add_column("invoices", sa.Column("buyer_registration_no_cache", sa.String(length=32), nullable=True))

    op.add_column("invoices", sa.Column("rounding_adjustment_cents", sa.Integer(), nullable=False, server_default="0"))

    op.add_column("invoices", sa.Column("issued_at", sa.DateTime(), nullable=True))
    op.add_column("invoices", sa.Column("sent_at", sa.DateTime(), nullable=True))
    op.add_column("invoices", sa.Column("paid_on", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("reminder_sent_at", sa.DateTime(), nullable=True))

    op.add_column("invoices", sa.Column("public_token", sa.String(length=128), nullable=True))
    op.create_index("ix_invoices_public_token", "invoices", ["public_token"], unique=True)

    op.add_column("invoices", sa.Column("pdf_path", sa.String(length=1024), nullable=True))
    op.add_column("invoices", sa.Column("pdf_hash", sa.String(length=128), nullable=True))
    op.add_column("invoices", sa.Column("pdf_generated_at", sa.DateTime(), nullable=True))

    op.add_column("invoices", sa.Column("series_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_invoices_series_id", "invoices", ["series_id"], unique=False)
    op.create_foreign_key(
        "fk_invoices_series_id",
        "invoices",
        "invoice_series",
        ["series_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index("ix_invoices_subject_status", "invoices", ["subject_id", "status"], unique=False)
    op.create_index("ix_invoices_subject_issue_date", "invoices", ["subject_id", "issue_date"], unique=False)

    # invoice_items: add net/vat columns
    op.add_column(
        "invoice_items",
        sa.Column("line_net_cents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "invoice_items",
        sa.Column("line_vat_cents", sa.Integer(), nullable=False, server_default="0"),
    )

    # --- Invoice parties (snapshot) --------------------------------------
    op.create_table(
        "invoice_parties",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("street", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("city", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("zip", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("country", sa.String(length=2), nullable=False, server_default="CZ"),
        sa.Column("ico", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("dic", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("invoice_id", "role", name="uq_invoice_party_role"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_invoice_parties_invoice_id", "invoice_parties", ["invoice_id"], unique=False)

    # --- Payments ---------------------------------------------------------
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"], unique=False)

    # --- Invoice emails ---------------------------------------------------
    op.create_table(
        "invoice_emails",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("to_email", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="sent"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_invoice_emails_invoice_id", "invoice_emails", ["invoice_id"], unique=False)

    # --- Audit log --------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_audit_log_subject_id", "audit_log", ["subject_id"], unique=False)
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"], unique=False)
    op.create_index("ix_audit_subject_created", "audit_log", ["subject_id", "created_at"], unique=False)

    # --- Import runs + map -----------------------------------------------
    op.create_table(
        "import_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="fakturoid"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_import_runs_subject_id", "import_runs", ["subject_id"], unique=False)

    op.create_table(
        "import_map",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="fakturoid"),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("internal_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "subject_id",
            "source",
            "entity_type",
            "external_id",
            name="uq_import_map_external",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_import_map_subject_id", "import_map", ["subject_id"], unique=False)
    op.create_index("ix_import_map_internal_id", "import_map", ["internal_id"], unique=False)

    # --- Company lookup cache --------------------------------------------
    op.create_table(
        "company_lookup_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("registration_no", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("country", "registration_no", name="uq_company_lookup"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_company_lookup_expires", "company_lookup_cache", ["expires_at"], unique=False)


def downgrade() -> None:
    # NOTE: Downgrade is best-effort for development. In production we would
    # normally not downgrade destructive schema changes.

    op.drop_index("ix_company_lookup_expires", table_name="company_lookup_cache")
    op.drop_table("company_lookup_cache")

    op.drop_index("ix_import_map_internal_id", table_name="import_map")
    op.drop_index("ix_import_map_subject_id", table_name="import_map")
    op.drop_table("import_map")

    op.drop_index("ix_import_runs_subject_id", table_name="import_runs")
    op.drop_table("import_runs")

    op.drop_index("ix_audit_subject_created", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_subject_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_invoice_emails_invoice_id", table_name="invoice_emails")
    op.drop_table("invoice_emails")

    op.drop_index("ix_payments_invoice_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_invoice_parties_invoice_id", table_name="invoice_parties")
    op.drop_table("invoice_parties")

    # Remove added columns/indexes from invoice_items
    op.drop_column("invoice_items", "line_vat_cents")
    op.drop_column("invoice_items", "line_net_cents")

    # Remove added columns/indexes from invoices
    op.drop_index("ix_invoices_subject_issue_date", table_name="invoices")
    op.drop_index("ix_invoices_subject_status", table_name="invoices")

    op.drop_constraint("fk_invoices_series_id", "invoices", type_="foreignkey")
    op.drop_index("ix_invoices_series_id", table_name="invoices")
    op.drop_column("invoices", "series_id")

    op.drop_column("invoices", "pdf_generated_at")
    op.drop_column("invoices", "pdf_hash")
    op.drop_column("invoices", "pdf_path")

    op.drop_index("ix_invoices_public_token", table_name="invoices")
    op.drop_column("invoices", "public_token")

    op.drop_column("invoices", "reminder_sent_at")
    op.drop_column("invoices", "paid_on")
    op.drop_column("invoices", "sent_at")
    op.drop_column("invoices", "issued_at")

    op.drop_column("invoices", "rounding_adjustment_cents")

    op.drop_column("invoices", "buyer_registration_no_cache")
    op.drop_column("invoices", "buyer_name_cache")
    op.drop_column("invoices", "internal_notes")

    op.drop_constraint("fk_invoices_subject_id", "invoices", type_="foreignkey")
    op.drop_index("ix_invoices_subject_id", table_name="invoices")
    op.drop_column("invoices", "subject_id")

    # Remove added columns/indexes from contacts
    op.drop_index("ix_contacts_subject_email", table_name="contacts")
    op.drop_index("ix_contacts_subject_name", table_name="contacts")

    op.drop_column("contacts", "external_id")
    op.drop_column("contacts", "external_source")

    op.drop_constraint("fk_contacts_subject_id", "contacts", type_="foreignkey")
    op.drop_index("ix_contacts_subject_id", table_name="contacts")
    op.drop_column("contacts", "subject_id")

    # Drop invoice_series
    op.drop_index("ix_invoice_series_subject_id", table_name="invoice_series")
    op.drop_table("invoice_series")

    # Drop RBAC tables
    op.drop_index("ix_user_subjects_subject_id", table_name="user_subjects")
    op.drop_index("ix_user_subjects_user_id", table_name="user_subjects")
    op.drop_table("user_subjects")

    op.drop_table("subjects")
    op.drop_table("users")

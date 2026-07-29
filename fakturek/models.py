from __future__ import annotations
from fakturek.time_utils import utc_now

from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fakturek.db import Base


BIGINT_SQLITE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class TimestampMixin:
    """Common timestamps.

    New writes use timezone-aware UTC timestamps. Some existing database
    backends/rows may still return naive UTC values; comparison code normalizes
    loaded timestamps with ``as_utc_aware`` where needed.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Identity / RBAC
# ---------------------------------------------------------------------------


class User(TimestampMixin, Base):
    """Login account.

    Multi-subject access is represented via :class:`UserSubject`.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    ui_theme: Mapped[str] = mapped_column(String(16), nullable=False, default="light")
    ui_language: Mapped[str] = mapped_column(String(8), nullable=False, default="cs")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    session_max_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_login_locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    subjects: Mapped[list["UserSubject"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    api_tokens: Mapped[list["ApiToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )


class Subject(TimestampMixin, Base):
    """Billing entity (seller)."""

    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    # Used in public invoice URL: /{username}/i/{token}/{invoice_number}
    public_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Seller profile (minimal; can grow)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    street: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    zip: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CZ")

    ico: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    dic: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    bank_account: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    is_vat_payer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_vat_identified_person: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_form: Mapped[str] = mapped_column(String(24), nullable=False, default="business")
    tax_regime: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    flat_tax_band: Mapped[str] = mapped_column(String(8), nullable=False, default="1")
    flat_tax_income_profile: Mapped[str] = mapped_column(String(24), nullable=False, default="general")
    tax_alerts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tax_alert_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vat_alert_last_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vat_alert_last_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flat_tax_alert_last_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flat_tax_alert_last_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")
    default_invoice_style: Mapped[str] = mapped_column(String(32), nullable=False, default="modern")
    invoice_pdf_theme: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    default_invoice_footer_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    default_invoice_footer_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    users: Mapped[list["UserSubject"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )

    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )

    series: Mapped[list["InvoiceSeries"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )

    bank_accounts: Mapped[list["SubjectBankAccount"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
        order_by="SubjectBankAccount.sort_order",
    )

    catalog_items: Mapped[list["InvoiceCatalogItem"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )

    api_tokens: Mapped[list["ApiToken"]] = relationship(
        back_populates="subject",
    )

    recurring_plans: Mapped[list["RecurringInvoicePlan"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
        order_by="RecurringInvoicePlan.next_issue_date",
    )

    __table_args__ = (
        sa.UniqueConstraint("public_username", name="uq_subjects_public_username"),
    )


class ApiToken(TimestampMixin, Base):
    """Personal access token for the JSON API.

    New tokens are scoped to a single subject. ``subject_id`` intentionally
    stays nullable as a transition aid for legacy installs until all existing
    tokens are migrated or recreated.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    subject_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship(back_populates="api_tokens")
    subject: Mapped[Subject | None] = relationship(back_populates="api_tokens")
    monthly_usages: Mapped[list["ApiTokenMonthlyUsage"]] = relationship(
        back_populates="token",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        sa.Index("ix_api_tokens_user_revoked", "user_id", "revoked_at"),
        sa.Index("ix_api_tokens_subject_revoked", "subject_id", "revoked_at"),
    )


class ApiTokenMonthlyUsage(TimestampMixin, Base):
    """Per-token monthly quota usage."""

    __tablename__ = "api_token_monthly_usage"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("api_tokens.id"),
        nullable=False,
        index=True,
    )
    usage_year: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_month: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    token: Mapped[ApiToken] = relationship(back_populates="monthly_usages")

    __table_args__ = (
        sa.UniqueConstraint(
            "token_id",
            "usage_year",
            "usage_month",
            name="uq_api_token_monthly_usage_scope",
        ),
        sa.Index(
            "ix_api_token_monthly_usage_lookup",
            "token_id",
            "usage_year",
            "usage_month",
        ),
    )


class ApiIdempotencyKey(TimestampMixin, Base):
    """Stored responses for idempotent API mutations."""

    __tablename__ = "api_idempotency_keys"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    subject_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=True,
        index=True,
    )

    request_method: Mapped[str] = mapped_column(String(12), nullable=False)
    request_path: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "user_id",
            "request_method",
            "request_path",
            "idempotency_key",
            name="uq_api_idempotency_scope",
        ),
        sa.Index(
            "ix_api_idempotency_lookup",
            "user_id",
            "request_method",
            "request_path",
            "idempotency_key",
        ),
    )


class UserSubject(TimestampMixin, Base):
    """RBAC link between a user and a subject."""

    __tablename__ = "user_subjects"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("users.id"), nullable=False, index=True)
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        index=True,
    )

    # Simple first-pass RBAC flags. FÁZE 3 will introduce middleware/dependencies.
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    can_view: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(back_populates="subjects")
    subject: Mapped[Subject] = relationship(back_populates="users")

    __table_args__ = (
        sa.UniqueConstraint("user_id", "subject_id", name="uq_user_subject"),
    )


# ---------------------------------------------------------------------------
# Legacy (pre-subject) issuer profile
# ---------------------------------------------------------------------------


class IssuerProfile(TimestampMixin, Base):
    """Legacy issuer profile.

    NOTE: This table is a temporary bridge from early MVP iterations.
    The master plan uses :class:`Subject` for seller data.
    """

    __tablename__ = "issuer_profiles"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    street: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    zip: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CZ")

    ico: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    dic: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    bank_account: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IssuerProfile id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    # Per-subject isolation (master plan). For early MVP routes we default to 1.
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        default=1,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    ico: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fixed_variable_symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)

    registry_auto_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registry_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registry_last_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registry_last_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registry_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    registry_data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registry_update_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    external_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    subject: Mapped[Subject] = relationship(back_populates="contacts")

    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.Index("ix_contacts_subject_name", "subject_id", "name"),
        sa.Index("ix_contacts_subject_email", "subject_id", "email"),
        sa.Index("ix_contacts_registry_auto", "subject_id", "registry_auto_update", "country", "ico"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Contact id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        default=1,
        index=True,
    )

    number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False, default="invoice", index=True)

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    taxable_supply_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")
    invoice_language: Mapped[str] = mapped_column(String(5), nullable=False, default="cs")
    invoice_style: Mapped[str] = mapped_column(String(32), nullable=False, default="modern")
    variable_symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False, default="bank_transfer")
    footer_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="trade_register")
    footer_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    contact_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("contacts.id"),
        nullable=False,
        index=True,
    )

    # Cached buyer fields for invoice list performance (filled from snapshot later).
    buyer_name_cache: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_registration_no_cache: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Totals (kept compatible with early MVP; later phases will split net/vat/gross).
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rounding_adjustment_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Lifecycle timestamps.
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Public invoice
    public_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    public_view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    public_last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Persisted issued PDF
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pdf_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Optional link to a numbering series.
    series_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoice_series.id"),
        nullable=True,
        index=True,
    )
    source_invoice_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=True,
        index=True,
    )

    bank_account_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subject_bank_accounts.id"),
        nullable=True,
        index=True,
    )
    bank_account_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bank_account_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bank_account_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bank_account_bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    bank_account_country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    subject: Mapped[Subject] = relationship(back_populates="invoices")
    contact: Mapped[Contact] = relationship(back_populates="invoices")

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceItem.sort_order",
    )

    parties: Mapped[list["InvoiceParty"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    payments: Mapped[list["Payment"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    emails: Mapped[list["InvoiceEmail"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    series: Mapped[InvoiceSeries | None] = relationship(back_populates="invoices")
    bank_account: Mapped["SubjectBankAccount | None"] = relationship(back_populates="invoices")
    source_invoice: Mapped["Invoice | None"] = relationship(remote_side="Invoice.id")
    recurring_plans: Mapped[list["RecurringInvoicePlan"]] = relationship(
        back_populates="template_invoice",
        cascade="all, delete-orphan",
        foreign_keys="RecurringInvoicePlan.template_invoice_id",
    )

    __table_args__ = (
        sa.UniqueConstraint("subject_id", "number", name="uq_invoices_subject_number"),
        sa.Index("ix_invoices_subject_status", "subject_id", "status"),
        sa.Index("ix_invoices_subject_type", "subject_id", "document_type"),
        sa.Index("ix_invoices_subject_issue_date", "subject_id", "issue_date"),
        sa.Index("ix_invoices_subject_taxable_supply_date", "subject_id", "taxable_supply_date"),
        sa.Index("ix_invoices_public_token", "public_token", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Invoice id={self.id} number={self.number!r}>"


class InvoiceParty(TimestampMixin, Base):
    """Invoice party snapshot (buyer/seller).

    In later phases, invoices become immutable after issue.
    """

    __tablename__ = "invoice_parties"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(String(10), nullable=False)  # 'buyer'|'seller'

    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    street: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    zip: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CZ")

    ico: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    dic: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    invoice: Mapped[Invoice] = relationship(back_populates="parties")

    __table_args__ = (
        sa.UniqueConstraint("invoice_id", "role", name="uq_invoice_party_role"),
    )


class InvoiceItem(TimestampMixin, Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    invoice_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    description: Mapped[str] = mapped_column(String(255), nullable=False)

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("1.00"),
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vat_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0.00"),
    )

    # Early MVP kept only `line_total_cents`. We add net/vat columns for later phases.
    line_net_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_vat_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    invoice: Mapped[Invoice] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InvoiceItem id={self.id} invoice_id={self.invoice_id} desc={self.description!r}>"
        )


class InvoiceCatalogItem(TimestampMixin, Base):
    __tablename__ = "invoice_catalog_items"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        index=True,
    )

    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("1.00"),
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vat_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")

    subject: Mapped[Subject] = relationship(back_populates="catalog_items")

    __table_args__ = (
        sa.Index("ix_invoice_catalog_items_subject_currency", "subject_id", "currency"),
        sa.Index("ix_invoice_catalog_items_subject_description", "subject_id", "description"),
    )


class RecurringInvoicePlan(TimestampMixin, Base):
    __tablename__ = "recurring_invoice_plans"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        index=True,
    )
    template_invoice_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    interval_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    interval_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_in_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_override: Mapped[str | None] = mapped_column(String(255), nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_generated_invoice_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=True,
        index=True,
    )

    subject: Mapped[Subject] = relationship(back_populates="recurring_plans")
    template_invoice: Mapped[Invoice] = relationship(
        back_populates="recurring_plans",
        foreign_keys=[template_invoice_id],
    )

    __table_args__ = (
        sa.Index("ix_recurring_plans_subject_active_date", "subject_id", "is_active", "next_issue_date"),
    )


class InvoiceSeries(TimestampMixin, Base):
    __tablename__ = "invoice_series"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    prefix: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    pad_length: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    last_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_counter_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    subject: Mapped[Subject] = relationship(back_populates="series")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="series")

    __table_args__ = (
        sa.UniqueConstraint("subject_id", "name", name="uq_invoice_series_subject_name"),
    )


class SubjectBankAccount(TimestampMixin, Base):
    __tablename__ = "subject_bank_accounts"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subjects.id"),
        nullable=False,
        index=True,
    )

    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    account_number: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CZ")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payment_sync_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    payment_sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_sync_auto_pair: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fio_api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_sync_alert_localpart: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payment_sync_email_sender_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_sync_email_subject_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_sync_email_parser: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    payment_sync_last_email_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_sync_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_sync_last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_sync_cursor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_sync_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    subject: Mapped[Subject] = relationship(back_populates="bank_accounts")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="bank_account")
    imported_transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="bank_account",
        cascade="all, delete-orphan",
        order_by="BankTransaction.booked_on.desc()",
    )
    incoming_emails: Mapped[list["BankIncomingEmail"]] = relationship(
        back_populates="bank_account",
        cascade="all, delete-orphan",
        order_by="BankIncomingEmail.received_at.desc()",
    )

    __table_args__ = (
        sa.Index("ix_subject_bank_accounts_subject_default", "subject_id", "is_default"),
    )




class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")
    bank_transactions: Mapped[list["BankTransaction"]] = relationship(back_populates="payment")
















class BankTransaction(TimestampMixin, Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_bank_account_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subject_bank_accounts.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="fio_api")
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    booked_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CZK")
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="incoming")
    variable_symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    constant_symbol: Mapped[str | None] = mapped_column(String(4), nullable=True)
    specific_symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    counterparty_account: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_invoice_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=True,
        index=True,
    )
    payment_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    bank_account: Mapped[SubjectBankAccount] = relationship(back_populates="imported_transactions")
    matched_invoice: Mapped[Invoice | None] = relationship()
    payment: Mapped[Payment | None] = relationship(back_populates="bank_transactions")

    __table_args__ = (
        sa.UniqueConstraint(
            "subject_bank_account_id",
            "provider",
            "external_id",
            name="uq_bank_transactions_account_provider_external",
        ),
        sa.Index(
            "ix_bank_transactions_account_booked_on",
            "subject_bank_account_id",
            "booked_on",
        ),
    )


class BankIncomingEmail(TimestampMixin, Base):
    __tablename__ = "bank_incoming_emails"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_bank_account_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("subject_bank_accounts.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="email_bank")
    imap_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    from_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="stored")
    processing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_bank_transaction_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("bank_transactions.id"),
        nullable=True,
        index=True,
    )

    bank_account: Mapped[SubjectBankAccount] = relationship(back_populates="incoming_emails")
    matched_bank_transaction: Mapped[BankTransaction | None] = relationship()

    __table_args__ = (
        sa.UniqueConstraint(
            "subject_bank_account_id",
            "provider",
            "imap_uid",
            name="uq_bank_incoming_emails_account_provider_uid",
        ),
        sa.Index(
            "ix_bank_incoming_emails_account_received_at",
            "subject_bank_account_id",
            "received_at",
        ),
    )


class InvoiceEmail(TimestampMixin, Base):
    __tablename__ = "invoice_emails"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    # Email purpose/type for UI and reporting.
    # Phase-23 introduces reminders; keep the field flexible for later types.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="invoice", index=True)

    # Sender address used for the SMTP envelope/header.
    # Stored for auditability/debugging.
    from_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Status is intentionally a free string ("queued"|"sent"|"error" for now).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # SMTP/Message metadata (best-effort).
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="emails")


class AuditLog(Base):
    """Audit log entry.

    We intentionally keep this table append-only.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    subject_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE, ForeignKey("subjects.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE, ForeignKey("users.id"), nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE, nullable=True)

    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        sa.Index("ix_audit_subject_created", "subject_id", "created_at"),
    )





class ImportRun(TimestampMixin, Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("subjects.id"), nullable=False, index=True)

    source: Mapped[str] = mapped_column(String(50), nullable=False, default="fakturoid")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase-24: uploaded import file metadata (for auditability and idempotence).
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Relative path under IMPORT_STORAGE_DIR. Empty until the file is stored.
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    file_size_bytes: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False, default=0)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")


class ImportMap(Base):
    __tablename__ = "import_map"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    subject_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("subjects.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="fakturoid")

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    internal_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False, index=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "subject_id",
            "source",
            "entity_type",
            "external_id",
            name="uq_import_map_external",
        ),
    )


class CompanyLookupCache(TimestampMixin, Base):
    __tablename__ = "company_lookup_cache"

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)

    country: Mapped[str] = mapped_column(String(2), nullable=False)
    registration_no: Mapped[str] = mapped_column(String(32), nullable=False)

    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("country", "registration_no", name="uq_company_lookup"),
        sa.Index("ix_company_lookup_expires", "expires_at"),
    )

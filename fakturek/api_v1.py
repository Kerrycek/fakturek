from __future__ import annotations
from fakturek.time_utils import as_utc_aware, utc_now

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from fakturek.api_tokens import hash_api_token_value
from fakturek.bank_sync import (
    EMAIL_BANK_PARSER_OPTIONS,
    BankSyncError,
    ImportedBankEmail,
    ImportedBankTransaction,
    extract_bank_email_recipients,
    fetch_fio_transactions,
    fetch_imap_bank_emails,
    parse_csas_cz_email,
    parse_csob_cz_email,
    parse_fio_email_cz,
    parse_raiffeisenbank_cz_email,
    safe_bank_sync_error_message,
)
from fakturek.banking import (
    BankAccountPayload,
    compute_rounding_adjustment_cents,
    digits_only,
    format_iban_for_display,
    normalize_spaces,
    resolve_bank_account,
    variable_symbol_from_invoice_number,
)
from fakturek.db import get_db
from fakturek.emailing import SMTPConfig, build_email_message, looks_like_email, send_via_smtp, split_recipients
from fakturek.models import (
    ApiIdempotencyKey,
    ApiToken,
    ApiTokenMonthlyUsage,
    BankIncomingEmail,
    BankTransaction,
    Contact,
    Invoice,
    InvoiceCatalogItem,
    InvoiceEmail,
    InvoiceItem,
    InvoiceParty,
    InvoiceSeries,
    Payment,
    RecurringInvoicePlan,
    Subject,
    SubjectBankAccount,
    User,
    UserSubject,
)
from fakturek.money import (
    compute_line_amounts_cents,
    format_cents,
    parse_money_to_cents,
    parse_money_to_signed_cents,
    parse_quantity,
    parse_vat_rate,
)
from fakturek.pdf import InvoicePDFData, render_invoice_pdf_bytes
from fakturek.pdf_store import persist_pdf_bytes, read_pdf_bytes, resolve_storage_root, safe_filename_base
from fakturek.rate_limit import SlidingWindowRateLimiter
from fakturek.public_links import (
    build_public_invoice_urls,
    ensure_invoice_public_link,
    generate_unique_invoice_public_token,
    resolve_public_base_url,
)
from fakturek.security import decrypt_secret
from fakturek.settings import Settings


bearer_scheme = HTTPBearer(auto_error=False)
API_MONTHLY_QUOTA_TZ = ZoneInfo("Europe/Prague")

VALID_PAYMENT_METHODS = {"bank_transfer", "cash", "card", "cod"}
VALID_DOCUMENT_TYPES = {"invoice", "quote", "credit_note", "proforma"}
VALID_INVOICE_STYLES = {"modern", "classic", "minimal"}
VALID_INVOICE_LANGUAGES = {"cs", "en"}
VALID_FOOTER_MODES = {"trade_register", "commercial_register", "association_register", "custom", "none"}
VALID_PAYMENT_SYNC_PROVIDERS = {"none", "fio_api", "email_bank"}
VALID_EMAIL_BANK_PARSERS = {value for value, _label in EMAIL_BANK_PARSER_OPTIONS}
BANK_SYNC_OVERLAP_DAYS = 3
EMAIL_BANK_PARSER_DEFAULTS: dict[str, dict[str, str]] = {
    "csas_cz": {
        "sender": "ceskasporitelna@csas.cz",
        "subject": "Přišla platba",
        "description": "Notifikace o příchozí platbě z České spořitelny.",
    },
    "raiffeisenbank_cz": {
        "sender": "info@rb.cz",
        "subject": "Pohyb na účtě",
        "description": "Notifikace o příchozí platbě z Raiffeisenbank.",
    },
    "csob_cz": {
        "sender": "noreply@csob.cz",
        "subject": "Moje info - Avízo",
        "description": "Avízo o zaúčtované příchozí platbě z ČSOB.",
    },
    "fio_email_cz": {
        "sender": "automat@fio.cz",
        "subject": "Fio banka - prijem na konte",
        "description": "Jednoduchý textový e-mail o příjmu na účet od Fio banky.",
    },
}
FOOTER_PRESET_TEXTS: dict[str, str] = {
    "trade_register": "Fyzická osoba zapsaná v živnostenském rejstříku.",
    "commercial_register": "Společnost zapsaná v obchodním rejstříku.",
    "association_register": "Spolek zapsaný ve spolkovém rejstříku.",
    "custom": "",
    "none": "",
}
VALID_RECURRING_INTERVAL_UNITS = {"week", "month"}
RECURRING_MONTH_LABELS = [
    "leden",
    "únor",
    "březen",
    "duben",
    "květen",
    "červen",
    "červenec",
    "srpen",
    "září",
    "říjen",
    "listopad",
    "prosinec",
]


def _request_scope_path(request: Request) -> str:
    """Return the canonical ASGI path for request identity checks."""

    try:
        path = request.scope.get("path")
    except Exception:
        path = None
    current = str(path or "").strip()
    if current.startswith("/"):
        return current
    try:
        fallback = str(request.url.path or "").strip()
    except Exception:
        fallback = ""
    if fallback.startswith("/"):
        return fallback
    return "/"


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.details = details or {}
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}
        super().__init__(message)


@dataclass(slots=True)
class ApiActor:
    user: User
    token: ApiToken
    subject: Subject
    link: UserSubject


@dataclass(slots=True)
class SubjectAccess:
    subject: Subject
    link: UserSubject


class SubjectPermissionsModel(BaseModel):
    role: str
    can_view: bool
    can_edit: bool
    can_issue: bool
    can_export: bool


class SubjectSummaryModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    public_username: str | None
    email: str
    phone: str
    ico: str
    dic: str
    country: str
    city: str
    zip: str
    street: str
    default_currency: str
    is_vat_payer: bool
    tax_regime: str
    permissions: SubjectPermissionsModel


class UserSummaryModel(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool


class ApiTokenSummaryModel(BaseModel):
    id: int
    name: str
    token_prefix: str
    subject_id: int
    subject_name: str
    subject_ico: str | None
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    can_read: bool
    can_write: bool
    can_issue: bool
    can_export: bool
    is_sandbox: bool


class DeleteResultModel(BaseModel):
    deleted: bool
    id: int


class InvoiceSeriesModel(BaseModel):
    id: int
    subject_id: int
    name: str
    prefix: str
    pad_length: int
    last_counter: int
    last_counter_year: int | None
    next_number_preview: str


class InvoiceSeriesListResponse(BaseModel):
    items: list[InvoiceSeriesModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class BankAccountModel(BaseModel):
    id: int
    subject_id: int
    label: str
    account_number: str | None
    iban: str | None
    iban_display: str | None
    bic: str | None
    country: str
    currency: str
    display_account: str
    is_default: bool
    sort_order: int
    payment_sync_provider: str
    payment_sync_enabled: bool
    payment_sync_auto_pair: bool
    payment_sync_last_checked_at: str | None
    payment_sync_last_success_at: str | None
    payment_sync_last_error: str | None


class BankAccountListResponse(BaseModel):
    items: list[BankAccountModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class CatalogItemModel(BaseModel):
    id: int
    subject_id: int
    description: str
    quantity: str
    unit: str
    unit_price: str
    vat_rate: str
    currency: str
    created_at: str
    updated_at: str


class CatalogItemListResponse(BaseModel):
    items: list[CatalogItemModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class CatalogItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: str = "1.00"
    unit: str = ""
    unit_price: str = "0.00"
    vat_rate: str | None = None
    currency: str | None = None


class CatalogItemPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: str | None = None
    unit: str | None = None
    unit_price: str | None = None
    vat_rate: str | None = None
    currency: str | None = None


class ContactModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_id: int
    name: str
    email: str | None
    phone: str | None
    street: str | None
    city: str | None
    zip: str | None
    country: str | None
    ico: str | None
    dic: str | None
    fixed_variable_symbol: str | None
    external_source: str | None
    external_id: str | None
    created_at: str
    updated_at: str


class ContactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    zip: str | None = None
    country: str | None = None
    ico: str | None = None
    dic: str | None = None
    fixed_variable_symbol: str | None = None


class ContactPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    zip: str | None = None
    country: str | None = None
    ico: str | None = None
    dic: str | None = None
    fixed_variable_symbol: str | None = None


class InvoicePublicLinkModel(BaseModel):
    enabled: bool
    url: str | None
    short_url: str | None
    pdf_url: str | None = None
    pdf_download_url: str | None = None
    isdoc_url: str | None = None
    isdoc_download_url: str | None = None


class InvoicePublicLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotate: bool = False


class InvoiceContactRefModel(BaseModel):
    id: int | None
    name: str


class InvoiceItemModel(BaseModel):
    id: int
    description: str
    quantity: str
    unit: str
    unit_price: str
    vat_rate: str
    line_net: str
    line_vat: str
    line_total: str
    sort_order: int


class InvoicePartyModel(BaseModel):
    role: str
    name: str
    email: str
    phone: str
    street: str
    city: str
    zip: str
    country: str
    ico: str
    dic: str


class PaymentModel(BaseModel):
    id: int
    paid_on: str
    amount: str
    note: str | None
    created_at: str
    bank_transaction_ids: list[int] = Field(default_factory=list)


class PaymentListResponse(BaseModel):
    items: list[PaymentModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class PaymentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_on: date | None = None
    amount: str | None = None
    note: str | None = None
    bank_transaction_id: int | None = None


class PaymentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_on: date | None = None
    amount: str | None = None
    note: str | None = None


class BankTransactionModel(BaseModel):
    id: int
    subject_bank_account_id: int
    bank_account_label: str | None
    provider: str
    external_id: str
    booked_on: str
    amount: str
    currency: str
    direction: str
    variable_symbol: str | None
    constant_symbol: str | None
    specific_symbol: str | None
    counterparty_account: str | None
    counterparty_name: str | None
    message: str | None
    matched_invoice_id: int | None
    matched_invoice_number: str | None
    payment_id: int | None
    matched_at: str | None
    created_at: str


class BankTransactionListResponse(BaseModel):
    items: list[BankTransactionModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class BankTransactionMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int
    note: str | None = None


class BankTransactionMatchResponse(BaseModel):
    transaction: BankTransactionModel
    payment: PaymentModel
    invoice_status: str


class BankTransactionUnmatchResponse(BaseModel):
    transaction: BankTransactionModel
    deleted_payment_id: int | None
    invoice_status: str


class RetryBankTransactionMatchResponse(BaseModel):
    bank_account_id: int
    inspected: int
    matched: int
    remaining_unmatched: int


class BankIncomingEmailModel(BaseModel):
    id: int
    subject_bank_account_id: int
    bank_account_label: str | None
    provider: str
    external_message_id: str | None
    received_at: str | None
    from_email: str | None
    subject: str | None
    processing_status: str
    processing_note: str | None
    matched_bank_transaction_id: int | None
    body_preview: str | None
    created_at: str


class BankIncomingEmailListResponse(BaseModel):
    items: list[BankIncomingEmailModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class BankIncomingEmailImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_message_id: str | None = None
    received_at: datetime | None = None
    from_email: str | None = None
    subject: str | None = None
    body_text: str
    parser: str | None = None
    auto_pair: bool | None = None


class BankIncomingEmailReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parser: str | None = None
    auto_pair: bool | None = None


class BankIncomingEmailImportResponse(BaseModel):
    email: BankIncomingEmailModel
    transaction: BankTransactionModel | None
    matched: bool


class BankTransactionImportItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    booked_on: date
    amount: str
    currency: str | None = None
    direction: str = "incoming"
    variable_symbol: str | None = None
    constant_symbol: str | None = None
    specific_symbol: str | None = None
    counterparty_account: str | None = None
    counterparty_name: str | None = None
    message: str | None = None


class BankTransactionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BankTransactionImportItemRequest] = Field(default_factory=list)
    auto_pair: bool | None = None


class BankTransactionImportItemResult(BaseModel):
    external_id: str
    result: str
    matched: bool
    transaction: BankTransactionModel | None = None
    message: str | None = None


class BankTransactionImportResponse(BaseModel):
    bank_account_id: int
    requested_count: int
    imported_count: int
    matched_count: int
    skipped_existing_count: int
    items: list[BankTransactionImportItemResult]


class BankSyncRunAccountModel(BaseModel):
    bank_account_id: int
    provider: str
    fetched: int
    imported: int
    matched: int
    unmatched: int
    skipped_existing: int
    baseline_seeded: bool = False
    errors: list[str] = Field(default_factory=list)


class BankSyncRunResponse(BaseModel):
    subject_id: int
    fetched: int
    imported: int
    matched: int
    unmatched: int
    skipped_existing: int
    baseline_seeded: bool = False
    errors: list[str] = Field(default_factory=list)
    accounts: list[BankSyncRunAccountModel] = Field(default_factory=list)


class BulkInvoiceActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    invoice_ids: list[int] = Field(default_factory=list)
    paid_on: date | None = None


class BulkInvoiceActionItem(BaseModel):
    invoice_id: int
    number: str | None
    from_status: str | None
    to_status: str | None
    result: str
    message: str | None = None


class BulkInvoiceActionResponse(BaseModel):
    action: str
    requested_count: int
    changed_count: int
    skipped_count: int
    deleted_count: int
    items: list[BulkInvoiceActionItem]


class InvoiceEmailModel(BaseModel):
    id: int
    kind: str
    from_email: str
    to_email: str
    subject: str
    status: str
    sent_at: str | None
    message_id: str | None
    error_message: str | None
    created_at: str


class InvoiceEmailListResponse(BaseModel):
    items: list[InvoiceEmailModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class InvoiceSendEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str | None = None
    cc: str | None = None
    subject: str | None = None
    body: str | None = None
    attach_pdf: bool = True
    include_public_link: bool = True


class InvoiceSendEmailResponse(BaseModel):
    email: InvoiceEmailModel
    invoice_status: str
    attached_pdf: bool
    public_link_included: bool


class InvoiceSummaryModel(BaseModel):
    id: int
    subject_id: int
    number: str
    status: str
    document_type: str
    issue_date: str
    due_date: str
    currency: str
    total: str
    discount: str
    rounding_adjustment: str
    variable_symbol: str | None
    payment_method: str
    invoice_language: str
    invoice_style: str
    issued_at: str | None
    sent_at: str | None
    paid_on: str | None
    pdf_available: bool
    contact: InvoiceContactRefModel
    public_link: InvoicePublicLinkModel


class InvoiceDetailModel(InvoiceSummaryModel):
    notes: str | None
    internal_notes: str | None
    source_invoice_id: int | None
    series_id: int | None
    footer_mode: str
    footer_text: str | None
    bank_account_id: int | None
    bank_account_label: str | None
    bank_account_number: str | None
    bank_account_iban: str | None
    bank_account_bic: str | None
    bank_account_country: str | None
    items: list[InvoiceItemModel]
    parties: list[InvoicePartyModel]
    payments: list[PaymentModel]
    created_at: str
    updated_at: str


class InvoiceItemWriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    quantity: str = "1.00"
    unit: str = ""
    unit_price: str = "0.00"
    vat_rate: str | None = None


class InvoiceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int
    issue_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    document_type: str = "invoice"
    source_invoice_id: int | None = None
    series_id: int | None = None
    bank_account_id: int | None = None
    payment_method: str | None = "bank_transfer"
    invoice_language: str | None = Field(default=None, validation_alias=AliasChoices("invoice_language", "language"))
    invoice_style: str | None = Field(default=None, validation_alias=AliasChoices("invoice_style", "style"))
    variable_symbol: str | None = None
    notes: str | None = None
    internal_notes: str | None = None
    footer_mode: str | None = None
    footer_text: str | None = None
    discount: str | None = "0.00"
    rounding_adjustment: str | None = "0.00"
    apply_auto_rounding: bool = False
    items: list[InvoiceItemWriteModel] = Field(default_factory=list)


class InvoicePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int | None = None
    issue_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    document_type: str | None = None
    source_invoice_id: int | None = None
    series_id: int | None = None
    bank_account_id: int | None = None
    payment_method: str | None = None
    invoice_language: str | None = Field(default=None, validation_alias=AliasChoices("invoice_language", "language"))
    invoice_style: str | None = Field(default=None, validation_alias=AliasChoices("invoice_style", "style"))
    variable_symbol: str | None = None
    notes: str | None = None
    internal_notes: str | None = None
    footer_mode: str | None = None
    footer_text: str | None = None
    discount: str | None = None
    rounding_adjustment: str | None = None
    apply_auto_rounding: bool | None = None
    items: list[InvoiceItemWriteModel] | None = None


class InvoiceRefModel(BaseModel):
    id: int
    number: str
    document_type: str
    status: str


class RecurringPlanModel(BaseModel):
    id: int
    subject_id: int
    name: str
    template_invoice: InvoiceRefModel
    interval_unit: str
    interval_count: int
    next_issue_date: str
    due_in_days: int
    is_active: bool
    auto_issue: bool
    auto_send: bool
    email_override: str | None
    last_run_at: str | None
    last_generated_invoice: InvoiceRefModel | None
    created_at: str
    updated_at: str


class RecurringPlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_invoice_id: int
    name: str | None = None
    interval_unit: str = "month"
    interval_count: int = 1
    next_issue_date: date | None = None
    due_in_days: int = 14
    is_active: bool = True
    auto_issue: bool = True
    auto_send: bool = False
    email_override: str | None = None


class RecurringPlanPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_invoice_id: int | None = None
    name: str | None = None
    interval_unit: str | None = None
    interval_count: int | None = None
    next_issue_date: date | None = None
    due_in_days: int | None = None
    is_active: bool | None = None
    auto_issue: bool | None = None
    auto_send: bool | None = None
    email_override: str | None = None


class RecurringPlanDeleteResponse(BaseModel):
    deleted: bool
    id: int


class RecurringPlanRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = True


class RecurringPlanRunResponse(BaseModel):
    plan: RecurringPlanModel
    created: bool
    emailed: bool
    created_invoice: InvoiceDetailModel | None
    errors: list[str]


class PaginationModel(BaseModel):
    page: int
    per_page: int
    total_items: int
    total_pages: int


class SubjectListResponse(BaseModel):
    items: list[SubjectSummaryModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class ContactListResponse(BaseModel):
    items: list[ContactModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class InvoiceListResponse(BaseModel):
    items: list[InvoiceSummaryModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class SandboxInvoicePreviewResponse(BaseModel):
    sandbox: bool = True
    persisted: bool = False
    message: str
    invoice: InvoiceDetailModel


class RecurringPlanListResponse(BaseModel):
    items: list[RecurringPlanModel]
    page: int
    per_page: int
    total_items: int
    total_pages: int


class MeResponse(BaseModel):
    user: UserSummaryModel
    token: ApiTokenSummaryModel
    subjects: list[SubjectSummaryModel]


class HealthResponse(BaseModel):
    status: str
    version: str


class ErrorEnvelopeModel(BaseModel):
    error: dict[str, Any]
    request_id: str | None = None


class InvoiceListFilters(BaseModel):
    q: str | None = None
    status: str | None = None
    document_type: str | None = None
    contact_id: int | None = None
    overdue: bool | None = None
    issue_date_from: date | None = None
    issue_date_to: date | None = None
    page: int = 1
    per_page: int = 50


class ContactListFilters(BaseModel):
    q: str | None = None
    page: int = 1
    per_page: int = 50


class ApiV1Builder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.project_root = Path(__file__).resolve().parent.parent
        self.pdf_storage_root = resolve_storage_root(self.settings.pdf_storage_dir, project_root=self.project_root)
        self.api_rate_limiter = SlidingWindowRateLimiter(
            max_requests=int(getattr(self.settings, "api_rate_limit_max", 240) or 240),
            window_seconds=int(getattr(self.settings, "api_rate_limit_window_seconds", 60) or 60),
        )
        self.app = FastAPI(
            title="Fakturek API v1",
            version="1.0.0-phase8",
            docs_url=None,
            redoc_url=None,
            openapi_url="/openapi.json",
        )
        self._install_handlers()
        self._register_routes()
        self._install_openapi()

    def _install_handlers(self) -> None:
        @self.app.exception_handler(ApiError)
        async def _api_error_handler(request: Request, exc: ApiError):
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                    "request_id": getattr(request.state, "request_id", None),
                },
                headers=exc.headers,
            )

        @self.app.exception_handler(RequestValidationError)
        async def _validation_error_handler(request: Request, exc: RequestValidationError):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "validation_error",
                        "message": "Požadavek neprošel validací.",
                        "details": {"issues": exc.errors()},
                    },
                    "request_id": getattr(request.state, "request_id", None),
                },
            )

    def _install_openapi(self) -> None:
        app = self.app

        def custom_openapi() -> dict[str, Any]:
            if app.openapi_schema:
                return app.openapi_schema

            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=(
                    "Bearer token autentizace pro Fakturek API v1. "
                    "Každý API token je scoped na jeden konkrétní subject / IČO a "
                    "autentizované endpointy vrací také 429 při překročení krátkodobého "
                    "rate limitu nebo měsíční API kvóty."
                ),
                routes=app.routes,
            )

            components = schema.setdefault("components", {})
            security_schemes = components.setdefault("securitySchemes", {})
            for scheme in security_schemes.values():
                if isinstance(scheme, dict) and scheme.get("type") == "http" and scheme.get("scheme") == "bearer":
                    scheme["description"] = (
                        "Použij Bearer token vytvořený pro konkrétní subject / IČO. "
                        "Token nezdědí přístup k ostatním subjectům uživatele a "
                        "podléhá krátkodobému throttlingu i měsíční kvótě."
                    )

            for path_item in schema.get("paths", {}).values():
                if not isinstance(path_item, dict):
                    continue
                for operation in path_item.values():
                    if not isinstance(operation, dict) or not operation.get("security"):
                        continue
                    responses = operation.setdefault("responses", {})
                    responses.setdefault(
                        "429",
                        {
                            "description": "API rate limit exceeded",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorEnvelopeModel"}
                                }
                            },
                        },
                    )

            app.openapi_schema = schema
            return schema

        app.openapi = custom_openapi

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/healthz", response_model=HealthResponse, tags=["meta"], summary="API health")
        def healthz() -> HealthResponse:
            return HealthResponse(status="ok", version=app.version)

        @app.get(
            "/openapi.yaml",
            response_class=PlainTextResponse,
            include_in_schema=False,
            tags=["meta"],
        )
        def openapi_yaml() -> str:
            schema = app.openapi()
            return yaml.safe_dump(schema, sort_keys=False, allow_unicode=True)

        @app.get("/docs", include_in_schema=False)
        def docs() -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url="/api/v1/openapi.json",
                title=f"{app.title} - Swagger UI",
                swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
                swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
                swagger_favicon_url="/static/favicon.svg",
                oauth2_redirect_url="/api/v1/docs/oauth2-redirect",
            )

        @app.get("/docs/oauth2-redirect", include_in_schema=False)
        def swagger_ui_redirect() -> HTMLResponse:
            return get_swagger_ui_oauth2_redirect_html()

        @app.get(
            "/me",
            response_model=MeResponse,
            responses={401: {"model": ErrorEnvelopeModel}},
            tags=["auth"],
            summary="Who am I",
        )
        def me(
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> MeResponse:
            subjects = self._list_subjects_for_actor(actor)
            return MeResponse(
                user=UserSummaryModel(
                    id=int(actor.user.id),
                    username=str(actor.user.username or ""),
                    email=str(actor.user.email or ""),
                    is_active=bool(actor.user.is_active),
                ),
                token=ApiTokenSummaryModel(
                    id=int(actor.token.id),
                    name=str(actor.token.name or ""),
                    token_prefix=str(actor.token.token_prefix or ""),
                    subject_id=int(actor.subject.id),
                    subject_name=str(actor.subject.name or ""),
                    subject_ico=(str(actor.subject.ico or "").strip() or None),
                    created_at=self._dt(actor.token.created_at),
                    last_used_at=self._dt(actor.token.last_used_at),
                    expires_at=self._dt(actor.token.expires_at),
                    can_read=bool(getattr(actor.token, "can_read", False)),
                    can_write=bool(getattr(actor.token, "can_write", False)),
                    can_issue=bool(getattr(actor.token, "can_issue", False)),
                    can_export=bool(getattr(actor.token, "can_export", False)),
                    is_sandbox=bool(getattr(actor.token, "is_sandbox", False)),
                ),
                subjects=subjects,
            )

        @app.get(
            "/subjects",
            response_model=SubjectListResponse,
            responses={401: {"model": ErrorEnvelopeModel}},
            tags=["subjects"],
            summary="List accessible subjects",
        )
        def list_subjects(
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> SubjectListResponse:
            rows = self._list_subjects_for_actor(actor)
            total = len(rows)
            start = (page - 1) * per_page
            end = start + per_page
            items = rows[start:end]
            return SubjectListResponse(
                items=items,
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}",
            response_model=SubjectSummaryModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["subjects"],
            summary="Subject detail",
        )
        def get_subject(
            subject_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> SubjectSummaryModel:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            return self._serialize_subject(access.subject, access.link)

        @app.get(
            "/subjects/{subject_id}/invoice-series",
            response_model=InvoiceSeriesListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["master-data"],
            summary="List invoice series",
        )
        def list_invoice_series(
            subject_id: int,
            year: int | None = Query(None, ge=2000, le=9999),
            document_type: str | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceSeriesListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            normalized_document_type = self._normalize_invoice_document_type(document_type, strict=True) if document_type is not None else None
            stmt = select(InvoiceSeries).where(InvoiceSeries.subject_id == int(subject_id)).order_by(InvoiceSeries.name.asc(), InvoiceSeries.id.asc())
            count_stmt = select(func.count(InvoiceSeries.id)).where(InvoiceSeries.subject_id == int(subject_id))
            if normalized_document_type is not None:
                expected_name, _expected_prefix = self._invoice_series_definition_for_type(normalized_document_type)
                stmt = stmt.where(InvoiceSeries.name == expected_name)
                count_stmt = count_stmt.where(InvoiceSeries.name == expected_name)
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
            return InvoiceSeriesListResponse(
                items=[self._serialize_invoice_series(db, subject_id=subject_id, row=row, year=year) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}/invoice-series/{series_id}",
            response_model=InvoiceSeriesModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["master-data"],
            summary="Invoice series detail",
        )
        def get_invoice_series(
            subject_id: int,
            series_id: int,
            year: int | None = Query(None, ge=2000, le=9999),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceSeriesModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_invoice_series_for_subject(db, subject_id=subject_id, series_id=series_id)
            return self._serialize_invoice_series(db, subject_id=subject_id, row=row, year=year)

        @app.get(
            "/subjects/{subject_id}/bank-accounts",
            response_model=BankAccountListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["master-data"],
            summary="List subject bank accounts",
        )
        def list_bank_accounts(
            subject_id: int,
            currency: str | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankAccountListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            normalized_currency = self._normalize_currency(currency) if currency is not None else None
            stmt = select(SubjectBankAccount).where(SubjectBankAccount.subject_id == int(subject_id))
            count_stmt = select(func.count(SubjectBankAccount.id)).where(SubjectBankAccount.subject_id == int(subject_id))
            if normalized_currency is not None:
                stmt = stmt.where(SubjectBankAccount.currency == normalized_currency)
                count_stmt = count_stmt.where(SubjectBankAccount.currency == normalized_currency)
            stmt = stmt.order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
            return BankAccountListResponse(
                items=[self._serialize_bank_account(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}/bank-accounts/{bank_account_id}",
            response_model=BankAccountModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["master-data"],
            summary="Subject bank account detail",
        )
        def get_bank_account(
            subject_id: int,
            bank_account_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankAccountModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_bank_account_for_subject(db, subject_id=subject_id, bank_account_id=bank_account_id)
            return self._serialize_bank_account(row)

        @app.get(
            "/subjects/{subject_id}/catalog-items",
            response_model=CatalogItemListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["catalog"],
            summary="List catalog items",
        )
        def list_catalog_items(
            subject_id: int,
            q: str | None = Query(None, description="Search by description"),
            currency: str | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> CatalogItemListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            normalized_currency = self._normalize_currency(currency) if currency is not None else None
            term = str(q or "").strip()
            stmt = select(InvoiceCatalogItem).where(InvoiceCatalogItem.subject_id == int(subject_id))
            count_stmt = select(func.count(InvoiceCatalogItem.id)).where(InvoiceCatalogItem.subject_id == int(subject_id))
            if normalized_currency is not None:
                stmt = stmt.where(InvoiceCatalogItem.currency == normalized_currency)
                count_stmt = count_stmt.where(InvoiceCatalogItem.currency == normalized_currency)
            if term:
                pattern = f"%{term}%"
                cond = InvoiceCatalogItem.description.ilike(pattern)
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)
            stmt = stmt.order_by(InvoiceCatalogItem.updated_at.desc(), InvoiceCatalogItem.id.desc())
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
            return CatalogItemListResponse(
                items=[self._serialize_catalog_item(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.post(
            "/subjects/{subject_id}/catalog-items",
            response_model=CatalogItemModel,
            status_code=201,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["catalog"],
            summary="Create catalog item",
        )
        def create_catalog_item(
            subject_id: int,
            payload: CatalogItemCreateRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> CatalogItemModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            normalized = self._catalog_item_values_from_request(payload, subject=access.subject)
            row = InvoiceCatalogItem(subject_id=int(subject_id), **normalized)
            db.add(row)
            db.flush()
            response_model = self._serialize_catalog_item(row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=201,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_catalog_item(row)

        @app.get(
            "/subjects/{subject_id}/catalog-items/{item_id}",
            response_model=CatalogItemModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["catalog"],
            summary="Catalog item detail",
        )
        def get_catalog_item(
            subject_id: int,
            item_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> CatalogItemModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_catalog_item_for_subject(db, subject_id=subject_id, item_id=item_id)
            return self._serialize_catalog_item(row)

        @app.patch(
            "/subjects/{subject_id}/catalog-items/{item_id}",
            response_model=CatalogItemModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["catalog"],
            summary="Update catalog item",
        )
        def patch_catalog_item(
            subject_id: int,
            item_id: int,
            payload: CatalogItemPatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> CatalogItemModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
            )
            if replay is not None:
                return replay

            row = self._load_catalog_item_for_subject(db, subject_id=subject_id, item_id=item_id)
            updates = self._catalog_item_patch_values_from_request(payload, current=row, subject=access.subject)
            for key, value in updates.items():
                setattr(row, key, value)
            db.add(row)
            db.flush()
            response_model = self._serialize_catalog_item(row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_catalog_item(row)

        @app.delete(
            "/subjects/{subject_id}/catalog-items/{item_id}",
            response_model=DeleteResultModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}},
            tags=["catalog"],
            summary="Delete catalog item",
        )
        def delete_catalog_item(
            subject_id: int,
            item_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> DeleteResultModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay

            row = self._load_catalog_item_for_subject(db, subject_id=subject_id, item_id=item_id)
            deleted_id = int(row.id)
            db.delete(row)
            response_model = DeleteResultModel(deleted=True, id=deleted_id)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/recurring-plans",
            response_model=RecurringPlanListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="List recurring plans",
        )
        def list_recurring_plans(
            subject_id: int,
            active: bool | None = Query(None),
            template_invoice_id: int | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            stmt = (
                select(RecurringInvoicePlan)
                .options(joinedload(RecurringInvoicePlan.template_invoice))
                .where(RecurringInvoicePlan.subject_id == int(subject_id))
            )
            count_stmt = select(func.count(RecurringInvoicePlan.id)).where(RecurringInvoicePlan.subject_id == int(subject_id))
            if active is not None:
                stmt = stmt.where(RecurringInvoicePlan.is_active.is_(bool(active)))
                count_stmt = count_stmt.where(RecurringInvoicePlan.is_active.is_(bool(active)))
            if template_invoice_id is not None:
                stmt = stmt.where(RecurringInvoicePlan.template_invoice_id == int(template_invoice_id))
                count_stmt = count_stmt.where(RecurringInvoicePlan.template_invoice_id == int(template_invoice_id))
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(
                stmt.order_by(RecurringInvoicePlan.is_active.desc(), RecurringInvoicePlan.next_issue_date.asc(), RecurringInvoicePlan.id.asc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).unique().all()
            return RecurringPlanListResponse(
                items=[self._serialize_recurring_plan(db, row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}/recurring-plans/{plan_id}",
            response_model=RecurringPlanModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="Recurring plan detail",
        )
        def get_recurring_plan(
            subject_id: int,
            plan_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_recurring_plan_for_subject(db, subject_id=subject_id, plan_id=plan_id)
            return self._serialize_recurring_plan(db, row)

        @app.post(
            "/subjects/{subject_id}/recurring-plans",
            response_model=RecurringPlanModel,
            status_code=201,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="Create recurring plan",
        )
        def create_recurring_plan(
            subject_id: int,
            payload: RecurringPlanCreateRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            request_hash = self._request_hash(payload, exclude_unset=False)
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay

            template_invoice = self._load_recurring_template_invoice(
                db,
                subject_id=subject_id,
                invoice_id=int(payload.template_invoice_id),
            )
            auto_issue = bool(payload.auto_issue)
            auto_send = bool(payload.auto_send)
            if auto_send and not auto_issue:
                raise ApiError(422, "recurring_auto_send_requires_auto_issue", "Automatické odeslání vyžaduje automatické vystavení.", {"field": "auto_send"})
            row = RecurringInvoicePlan(
                subject_id=int(subject_id),
                template_invoice_id=int(template_invoice.id),
                name=self._resolve_recurring_plan_name(payload.name, template_invoice=template_invoice),
                interval_unit=self._normalize_recurring_interval_unit(payload.interval_unit),
                interval_count=self._normalize_recurring_interval_count(payload.interval_count),
                next_issue_date=payload.next_issue_date or date.today(),
                due_in_days=self._normalize_recurring_due_in_days(payload.due_in_days),
                is_active=bool(payload.is_active),
                auto_issue=auto_issue,
                auto_send=auto_send,
                email_override=self._normalize_recurring_email_override(payload.email_override),
            )
            db.add(row)
            db.flush()
            response_model = self._serialize_recurring_plan(db, row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=201,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_recurring_plan(db, row)

        @app.patch(
            "/subjects/{subject_id}/recurring-plans/{plan_id}",
            response_model=RecurringPlanModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="Update recurring plan",
        )
        def update_recurring_plan(
            subject_id: int,
            plan_id: int,
            payload: RecurringPlanPatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            request_hash = self._request_hash(payload, exclude_unset=True)
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay

            row = self._load_recurring_plan_for_subject(db, subject_id=subject_id, plan_id=plan_id)
            fields_set = set(payload.model_fields_set)
            if "template_invoice_id" in fields_set and payload.template_invoice_id is not None:
                template_invoice = self._load_recurring_template_invoice(
                    db,
                    subject_id=subject_id,
                    invoice_id=int(payload.template_invoice_id),
                )
                row.template_invoice_id = int(template_invoice.id)
                row.template_invoice = template_invoice
            auto_issue = bool(payload.auto_issue) if "auto_issue" in fields_set and payload.auto_issue is not None else bool(row.auto_issue)
            auto_send = bool(payload.auto_send) if "auto_send" in fields_set and payload.auto_send is not None else bool(row.auto_send)
            if auto_send and not auto_issue:
                raise ApiError(422, "recurring_auto_send_requires_auto_issue", "Automatické odeslání vyžaduje automatické vystavení.", {"field": "auto_send"})
            if "name" in fields_set:
                row.name = self._resolve_recurring_plan_name(payload.name, template_invoice=getattr(row, "template_invoice", None))
            if "interval_unit" in fields_set and payload.interval_unit is not None:
                row.interval_unit = self._normalize_recurring_interval_unit(payload.interval_unit)
            if "interval_count" in fields_set and payload.interval_count is not None:
                row.interval_count = self._normalize_recurring_interval_count(payload.interval_count)
            if "next_issue_date" in fields_set and payload.next_issue_date is not None:
                row.next_issue_date = payload.next_issue_date
            if "due_in_days" in fields_set and payload.due_in_days is not None:
                row.due_in_days = self._normalize_recurring_due_in_days(payload.due_in_days)
            if "is_active" in fields_set and payload.is_active is not None:
                row.is_active = bool(payload.is_active)
            row.auto_issue = auto_issue
            row.auto_send = auto_send
            if "email_override" in fields_set:
                row.email_override = self._normalize_recurring_email_override(payload.email_override)
            db.add(row)
            db.flush()
            response_model = self._serialize_recurring_plan(db, row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_recurring_plan(db, row)

        @app.delete(
            "/subjects/{subject_id}/recurring-plans/{plan_id}",
            response_model=RecurringPlanDeleteResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="Delete recurring plan",
        )
        def delete_recurring_plan(
            subject_id: int,
            plan_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanDeleteResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            request_hash = self._request_hash({}, exclude_unset=False)
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay

            row = self._load_recurring_plan_for_subject(db, subject_id=subject_id, plan_id=plan_id)
            response_model = RecurringPlanDeleteResponse(deleted=True, id=int(row.id))
            db.delete(row)
            db.flush()
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/recurring-plans/{plan_id}/run",
            response_model=RecurringPlanRunResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["recurring"],
            summary="Run recurring plan now",
        )
        def run_recurring_plan(
            subject_id: int,
            plan_id: int,
            request: Request,
            payload: RecurringPlanRunRequest = RecurringPlanRunRequest(),
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RecurringPlanRunResponse | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            request_hash = self._request_hash(payload, exclude_unset=False)
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay

            plan = self._load_recurring_plan_for_subject(db, subject_id=subject_id, plan_id=plan_id)
            created_invoice, emailed, errors = self._run_recurring_plan_once(
                db,
                plan=plan,
                subject=access.subject,
                request=request,
                force=bool(payload.force),
            )
            db.flush()
            refreshed_plan = self._load_recurring_plan_for_subject(db, subject_id=subject_id, plan_id=plan_id)
            created_invoice_detail = (
                self._serialize_invoice_detail(
                    self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(created_invoice.id)),
                    request=request,
                )
                if created_invoice is not None
                else None
            )
            response_model = RecurringPlanRunResponse(
                plan=self._serialize_recurring_plan(db, refreshed_plan),
                created=bool(created_invoice is not None),
                emailed=bool(emailed),
                created_invoice=created_invoice_detail,
                errors=errors,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/contacts",
            response_model=ContactListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["contacts"],
            summary="List contacts",
        )
        def list_contacts(
            subject_id: int,
            q: str | None = Query(None, description="Search by name, email or IČO"),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> ContactListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            stmt = select(Contact).where(Contact.subject_id == int(subject_id)).order_by(Contact.name.asc(), Contact.id.asc())
            count_stmt = select(func.count(Contact.id)).where(Contact.subject_id == int(subject_id))
            term = str(q or "").strip()
            if term:
                pattern = f"%{term}%"
                cond = or_(
                    Contact.name.ilike(pattern),
                    Contact.email.ilike(pattern),
                    Contact.ico.ilike(pattern),
                )
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
            return ContactListResponse(
                items=[self._serialize_contact(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.post(
            "/subjects/{subject_id}/contacts",
            response_model=ContactModel,
            status_code=201,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["contacts"],
            summary="Create contact",
        )
        def create_contact(
            subject_id: int,
            payload: ContactCreateRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> ContactModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            normalized = self._contact_payload_from_request(payload)
            row = Contact(subject_id=int(subject_id), **normalized)
            db.add(row)
            db.flush()
            response_model = self._serialize_contact(row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=201,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_contact(row)

        @app.get(
            "/subjects/{subject_id}/contacts/{contact_id}",
            response_model=ContactModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["contacts"],
            summary="Contact detail",
        )
        def get_contact(
            subject_id: int,
            contact_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> ContactModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=contact_id)
            return self._serialize_contact(row)

        @app.patch(
            "/subjects/{subject_id}/contacts/{contact_id}",
            response_model=ContactModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["contacts"],
            summary="Update contact",
        )
        def update_contact(
            subject_id: int,
            contact_id: int,
            payload: ContactPatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> ContactModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
            )
            if replay is not None:
                return replay

            row = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=contact_id)
            updates = self._contact_patch_from_request(payload)
            for key, value in updates.items():
                setattr(row, key, value)
            db.add(row)
            db.flush()
            response_model = self._serialize_contact(row)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            db.refresh(row)
            return self._serialize_contact(row)

        @app.get(
            "/subjects/{subject_id}/invoices",
            response_model=InvoiceListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="List invoices",
        )
        def list_invoices(
            request: Request,
            subject_id: int,
            q: str | None = Query(None, description="Search by invoice number, buyer name or variable symbol"),
            status: str | None = Query(None),
            document_type: str | None = Query(None),
            contact_id: int | None = Query(None),
            overdue: bool | None = Query(None),
            issue_date_from: date | None = Query(None),
            issue_date_to: date | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            filters = InvoiceListFilters(
                q=q,
                status=status,
                document_type=document_type,
                contact_id=contact_id,
                overdue=overdue,
                issue_date_from=issue_date_from,
                issue_date_to=issue_date_to,
                page=page,
                per_page=per_page,
            )
            stmt = (
                select(Invoice)
                .options(joinedload(Invoice.contact), joinedload(Invoice.subject))
                .where(Invoice.subject_id == int(subject_id))
                .order_by(Invoice.issue_date.desc(), Invoice.id.desc())
            )
            count_stmt = select(func.count(Invoice.id)).where(Invoice.subject_id == int(subject_id))
            stmt, count_stmt = self._apply_invoice_filters(stmt, count_stmt, filters)
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).unique().all()
            return InvoiceListResponse(
                items=[self._serialize_invoice_summary(row, request=request) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.post(
            "/subjects/{subject_id}/invoices",
            response_model=InvoiceDetailModel,
            status_code=201,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Create draft invoice",
        )
        def create_invoice(
            subject_id: int,
            payload: InvoiceCreateRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceDetailModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            normalized_document_type = self._normalize_invoice_document_type(payload.document_type, strict=True)
            issue_date = payload.issue_date or date.today()
            due_date = payload.due_date or (issue_date + timedelta(days=14))
            currency = self._normalize_currency(payload.currency or access.subject.default_currency or "CZK")
            is_vat_payer = bool(access.subject.is_vat_payer)
            contact = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=payload.contact_id)
            items_payload = self._parse_invoice_items(
                payload.items,
                is_vat_payer=is_vat_payer,
                allow_negative_unit_price=normalized_document_type == "credit_note",
            )
            financials = self._resolve_invoice_financials(
                items_payload=items_payload,
                discount=payload.discount,
                rounding_adjustment=payload.rounding_adjustment,
                apply_auto_rounding=bool(payload.apply_auto_rounding),
            )
            source_invoice = self._resolve_invoice_source(
                db,
                subject_id=subject_id,
                document_type=normalized_document_type,
                source_invoice_id=payload.source_invoice_id,
                current_invoice_id=None,
            )
            self._validate_credit_note_amount(
                db,
                document_type=normalized_document_type,
                source_invoice=source_invoice,
                current_invoice_id=None,
                proposed_total_cents=financials["draft_total_cents"],
            )
            selected_series = self._resolve_invoice_series(
                db,
                subject_id=subject_id,
                document_type=normalized_document_type,
                requested_series_id=payload.series_id,
            )
            selected_bank_account = self._resolve_bank_account_selection(
                db,
                subject_id=subject_id,
                requested_bank_account_id=payload.bank_account_id,
                currency=currency,
            )
            payment_method = self._normalize_payment_method(payload.payment_method)
            invoice_language = self._normalize_invoice_language(payload.invoice_language)
            invoice_style = self._normalize_invoice_style(payload.invoice_style)
            footer_mode, footer_text = self._resolve_invoice_footer(
                subject=access.subject,
                footer_mode=payload.footer_mode,
                footer_text=payload.footer_text,
            )
            variable_symbol = self._resolve_invoice_variable_symbol(
                explicit_value=payload.variable_symbol,
                contact=contact,
            )

            invoice = Invoice(
                subject_id=int(subject_id),
                number="DRAFT-temp",
                status="draft",
                issue_date=issue_date,
                taxable_supply_date=issue_date,
                due_date=due_date,
                currency=currency,
                variable_symbol=variable_symbol,
                notes=self._coalesce_optional_text(payload.notes),
                internal_notes=self._coalesce_optional_text(payload.internal_notes),
                payment_method=payment_method,
                invoice_language=invoice_language,
                invoice_style=invoice_style,
                footer_mode=footer_mode,
                footer_text=footer_text,
                document_type=normalized_document_type,
                source_invoice_id=(int(source_invoice.id) if source_invoice is not None else None),
                contact_id=int(contact.id),
                buyer_name_cache=str(contact.name or "") or None,
                buyer_registration_no_cache=self._coalesce_optional_text(contact.ico),
                discount_cents=int(financials["discount_cents"]),
                rounding_adjustment_cents=int(financials["rounding_adjustment_cents"]),
                total_cents=int(financials["draft_total_cents"]),
                series_id=(int(selected_series.id) if selected_series is not None else None),
            )
            db.add(invoice)
            db.flush()
            invoice.number = f"DRAFT-{int(invoice.id)}"
            ensure_invoice_public_link(db, invoice=invoice, subject=access.subject)
            self._sync_invoice_parties(db, invoice=invoice, subject=access.subject, contact=contact, sync_existing=True)
            self._apply_invoice_bank_account_snapshot(
                invoice,
                account=selected_bank_account,
                subject=access.subject,
                allow_subject_fallback=selected_bank_account is None,
            )
            self._replace_invoice_items(db, invoice_id=int(invoice.id), items_payload=items_payload)
            self._recalc_invoice_total_cents(db, invoice=invoice)
            db.flush()
            response_model = self._serialize_invoice_detail(
                self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(invoice.id)),
                request=request,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=201,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/sandbox/invoices",
            response_model=SandboxInvoicePreviewResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["sandbox"],
            summary="Preview invoice creation without persisting it",
        )
        def sandbox_invoice_preview(
            subject_id: int,
            payload: InvoiceCreateRequest,
            request: Request,
            issue: bool = Query(True, description="When true, also simulate issuing and numbering the invoice."),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> SandboxInvoicePreviewResponse:
            if not bool(getattr(actor.token, "is_sandbox", False)):
                raise ApiError(
                    403,
                    "api_token_sandbox_required",
                    "Zkušební endpoint vyžaduje API klíč vytvořený pro zkušební prostředí.",
                    {"subject_id": int(subject_id)},
                )
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit", sandbox=True)
            if bool(issue):
                self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue", sandbox=True)

            try:
                normalized_document_type = self._normalize_invoice_document_type(payload.document_type, strict=True)
                issue_date = payload.issue_date or date.today()
                due_date = payload.due_date or (issue_date + timedelta(days=14))
                currency = self._normalize_currency(payload.currency or access.subject.default_currency or "CZK")
                is_vat_payer = bool(access.subject.is_vat_payer)
                contact = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=payload.contact_id)
                items_payload = self._parse_invoice_items(
                    payload.items,
                    is_vat_payer=is_vat_payer,
                    allow_negative_unit_price=normalized_document_type == "credit_note",
                )
                financials = self._resolve_invoice_financials(
                    items_payload=items_payload,
                    discount=payload.discount,
                    rounding_adjustment=payload.rounding_adjustment,
                    apply_auto_rounding=bool(payload.apply_auto_rounding),
                )
                source_invoice = self._resolve_invoice_source(
                    db,
                    subject_id=subject_id,
                    document_type=normalized_document_type,
                    source_invoice_id=payload.source_invoice_id,
                    current_invoice_id=None,
                )
                self._validate_credit_note_amount(
                    db,
                    document_type=normalized_document_type,
                    source_invoice=source_invoice,
                    current_invoice_id=None,
                    proposed_total_cents=financials["draft_total_cents"],
                )
                selected_series = self._resolve_invoice_series(
                    db,
                    subject_id=subject_id,
                    document_type=normalized_document_type,
                    requested_series_id=payload.series_id,
                )
                selected_bank_account = self._resolve_bank_account_selection(
                    db,
                    subject_id=subject_id,
                    requested_bank_account_id=payload.bank_account_id,
                    currency=currency,
                )
                payment_method = self._normalize_payment_method(payload.payment_method)
                invoice_language = self._normalize_invoice_language(payload.invoice_language)
                invoice_style = self._normalize_invoice_style(payload.invoice_style)
                footer_mode, footer_text = self._resolve_invoice_footer(
                    subject=access.subject,
                    footer_mode=payload.footer_mode,
                    footer_text=payload.footer_text,
                )
                variable_symbol = self._resolve_invoice_variable_symbol(
                    explicit_value=payload.variable_symbol,
                    contact=contact,
                )

                invoice = Invoice(
                    subject_id=int(subject_id),
                    number="DRAFT-sandbox",
                    status="draft",
                    issue_date=issue_date,
                    taxable_supply_date=issue_date,
                    due_date=due_date,
                    currency=currency,
                    variable_symbol=variable_symbol,
                    notes=self._coalesce_optional_text(payload.notes),
                    internal_notes=self._coalesce_optional_text(payload.internal_notes),
                    payment_method=payment_method,
                    invoice_language=invoice_language,
                    invoice_style=invoice_style,
                    footer_mode=footer_mode,
                    footer_text=footer_text,
                    document_type=normalized_document_type,
                    source_invoice_id=(int(source_invoice.id) if source_invoice is not None else None),
                    contact_id=int(contact.id),
                    buyer_name_cache=str(contact.name or "") or None,
                    buyer_registration_no_cache=self._coalesce_optional_text(contact.ico),
                    discount_cents=int(financials["discount_cents"]),
                    rounding_adjustment_cents=int(financials["rounding_adjustment_cents"]),
                    total_cents=int(financials["draft_total_cents"]),
                    series_id=(int(selected_series.id) if selected_series is not None else None),
                )
                db.add(invoice)
                db.flush()
                invoice.number = f"DRAFT-{int(invoice.id)}"
                ensure_invoice_public_link(db, invoice=invoice, subject=access.subject)
                self._sync_invoice_parties(db, invoice=invoice, subject=access.subject, contact=contact, sync_existing=True)
                self._apply_invoice_bank_account_snapshot(
                    invoice,
                    account=selected_bank_account,
                    subject=access.subject,
                    allow_subject_fallback=selected_bank_account is None,
                )
                self._replace_invoice_items(db, invoice_id=int(invoice.id), items_payload=items_payload)
                self._recalc_invoice_total_cents(db, invoice=invoice)
                db.flush()
                if bool(issue):
                    self._issue_invoice_draft(db, subject=access.subject, invoice=invoice)
                    db.flush()
                response_model = self._serialize_invoice_detail(
                    self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(invoice.id)),
                    request=request,
                )
                return SandboxInvoicePreviewResponse(
                    sandbox=True,
                    persisted=False,
                    message="Zkušební faktura byla jen nasimulovaná. V databázi se neuložila a číselná řada se nezměnila.",
                    invoice=response_model,
                )
            finally:
                db.rollback()

        @app.get(
            "/subjects/{subject_id}/invoices/{invoice_id}",
            response_model=InvoiceDetailModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Invoice detail",
        )
        def get_invoice(
            request: Request,
            subject_id: int,
            invoice_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceDetailModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            return self._serialize_invoice_detail(row, request=request)

        @app.patch(
            "/subjects/{subject_id}/invoices/{invoice_id}",
            response_model=InvoiceDetailModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Update draft invoice",
        )
        def update_invoice(
            subject_id: int,
            invoice_id: int,
            payload: InvoicePatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceDetailModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            if str(invoice.status or "draft") != "draft":
                raise ApiError(409, "invoice_not_draft", "Upravovat přes API lze v této fázi jen koncept dokladu.", {"invoice_id": int(invoice_id), "status": str(invoice.status or "")})

            fields_set = set(payload.model_fields_set)
            document_type = self._normalize_invoice_document_type(
                payload.document_type if "document_type" in fields_set else str(invoice.document_type or "invoice"),
                strict=True,
            )
            contact_id = int(payload.contact_id) if "contact_id" in fields_set and payload.contact_id is not None else int(invoice.contact_id)
            contact = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=contact_id)
            issue_date = payload.issue_date if "issue_date" in fields_set and payload.issue_date is not None else invoice.issue_date
            due_date = payload.due_date if "due_date" in fields_set and payload.due_date is not None else invoice.due_date
            currency = self._normalize_currency(payload.currency if "currency" in fields_set else invoice.currency)
            is_vat_payer = bool(access.subject.is_vat_payer)
            if "items" in fields_set:
                items_payload = self._parse_invoice_items(
                    payload.items or [],
                    is_vat_payer=is_vat_payer,
                    allow_negative_unit_price=document_type == "credit_note",
                )
                replace_items = True
                items_total_cents = sum(int(item.get("line_total_cents") or 0) for item in items_payload)
            else:
                items_payload = None
                replace_items = False
                items_total_cents = sum(int(getattr(item, "line_total_cents", 0) or 0) for item in invoice.items)

            discount_value = payload.discount if "discount" in fields_set else self._money(invoice.discount_cents)
            rounding_value = payload.rounding_adjustment if "rounding_adjustment" in fields_set else self._money(invoice.rounding_adjustment_cents)
            apply_auto_rounding = bool(payload.apply_auto_rounding) if payload.apply_auto_rounding is not None else False
            financials = self._resolve_invoice_financials(
                items_total_cents=items_total_cents,
                discount=discount_value,
                rounding_adjustment=rounding_value,
                apply_auto_rounding=apply_auto_rounding,
            )
            source_invoice = self._resolve_invoice_source(
                db,
                subject_id=subject_id,
                document_type=document_type,
                source_invoice_id=(payload.source_invoice_id if "source_invoice_id" in fields_set else invoice.source_invoice_id),
                current_invoice_id=int(invoice.id),
            )
            self._validate_credit_note_amount(
                db,
                document_type=document_type,
                source_invoice=source_invoice,
                current_invoice_id=int(invoice.id),
                proposed_total_cents=financials["draft_total_cents"],
            )
            if "series_id" in fields_set:
                selected_series = self._resolve_invoice_series(
                    db,
                    subject_id=subject_id,
                    document_type=document_type,
                    requested_series_id=payload.series_id,
                )
            else:
                selected_series = self._resolve_existing_series(db, subject_id=subject_id, series_id=invoice.series_id)
                if selected_series is None:
                    selected_series = self._resolve_invoice_series(
                        db,
                        subject_id=subject_id,
                        document_type=document_type,
                        requested_series_id=None,
                    )
            payment_method = self._normalize_payment_method(
                payload.payment_method if "payment_method" in fields_set else str(invoice.payment_method or "bank_transfer")
            )
            invoice_language = self._normalize_invoice_language(
                payload.invoice_language if "invoice_language" in fields_set else str(invoice.invoice_language or "cs")
            )
            invoice_style = self._normalize_invoice_style(
                payload.invoice_style if "invoice_style" in fields_set else str(getattr(invoice, "invoice_style", None) or "modern")
            )
            if self._contact_fixed_variable_symbol(contact):
                variable_symbol = self._contact_fixed_variable_symbol(contact)
            elif "variable_symbol" in fields_set:
                variable_symbol = self._normalize_variable_symbol_field(payload.variable_symbol)
            else:
                variable_symbol = self._normalize_variable_symbol_field(invoice.variable_symbol)
            footer_mode_input = payload.footer_mode if "footer_mode" in fields_set else invoice.footer_mode
            footer_text_input = payload.footer_text if "footer_text" in fields_set else invoice.footer_text
            footer_mode, footer_text = self._resolve_invoice_footer(
                subject=access.subject,
                footer_mode=footer_mode_input,
                footer_text=footer_text_input,
            )

            invoice.contact_id = int(contact.id)
            invoice.issue_date = issue_date
            invoice.due_date = due_date
            invoice.currency = currency
            invoice.document_type = document_type
            invoice.source_invoice_id = int(source_invoice.id) if source_invoice is not None else None
            invoice.variable_symbol = variable_symbol
            invoice.notes = self._coalesce_optional_text(payload.notes) if "notes" in fields_set else invoice.notes
            invoice.internal_notes = self._coalesce_optional_text(payload.internal_notes) if "internal_notes" in fields_set else invoice.internal_notes
            invoice.payment_method = payment_method
            invoice.invoice_language = invoice_language
            invoice.invoice_style = invoice_style
            invoice.footer_mode = footer_mode
            invoice.footer_text = footer_text
            invoice.discount_cents = int(financials["discount_cents"])
            invoice.rounding_adjustment_cents = int(financials["rounding_adjustment_cents"])
            invoice.buyer_name_cache = str(contact.name or "") or None
            invoice.buyer_registration_no_cache = self._coalesce_optional_text(contact.ico)
            invoice.series_id = int(selected_series.id) if selected_series is not None else None

            self._sync_invoice_parties(db, invoice=invoice, subject=access.subject, contact=contact, sync_existing=True)
            if "bank_account_id" in fields_set:
                if payload.bank_account_id is None:
                    invoice.bank_account_id = None
                    invoice.bank_account_label = None
                    invoice.bank_account_number = None
                    invoice.bank_account_iban = None
                    invoice.bank_account_bic = None
                    invoice.bank_account_country = None
                else:
                    selected_bank_account = self._resolve_bank_account_selection(
                        db,
                        subject_id=subject_id,
                        requested_bank_account_id=payload.bank_account_id,
                        currency=currency,
                    )
                    self._apply_invoice_bank_account_snapshot(
                        invoice,
                        account=selected_bank_account,
                        subject=access.subject,
                        allow_subject_fallback=True,
                    )
            if replace_items:
                self._replace_invoice_items(db, invoice_id=int(invoice.id), items_payload=items_payload or [])
            self._recalc_invoice_total_cents(db, invoice=invoice)
            db.flush()
            response_model = self._serialize_invoice_detail(
                self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(invoice.id)),
                request=request,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/invoices/{invoice_id}/issue",
            response_model=InvoiceDetailModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Issue draft invoice",
        )
        def issue_invoice(
            subject_id: int,
            invoice_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceDetailModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            self._issue_invoice_draft(db, subject=access.subject, invoice=invoice)
            response_model = self._serialize_invoice_detail(
                self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(invoice.id)),
                request=request,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/invoices/{invoice_id}/pdf",
            responses={
                200: {"content": {"application/pdf": {}}},
                401: {"model": ErrorEnvelopeModel},
                403: {"model": ErrorEnvelopeModel},
                404: {"model": ErrorEnvelopeModel},
                500: {"model": ErrorEnvelopeModel},
            },
            tags=["invoices"],
            summary="Render or download invoice PDF",
        )
        def invoice_pdf(
            subject_id: int,
            invoice_id: int,
            download: bool = Query(False),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> Response:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="export")
            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            pdf_bytes = self._invoice_pdf_bytes(db, invoice=invoice)
            db.commit()
            filename = f"{safe_filename_base(str(invoice.number or ''), fallback=f'invoice-{int(invoice.id)}')}.pdf"
            disposition = "attachment" if bool(download) else "inline"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'{disposition}; filename="{filename}"',
                },
            )

        @app.get(
            "/subjects/{subject_id}/invoices/{invoice_id}/emails",
            response_model=InvoiceEmailListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="List invoice email log",
        )
        def list_invoice_emails(
            subject_id: int,
            invoice_id: int,
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceEmailListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            total = int(
                db.scalar(
                    select(func.count(InvoiceEmail.id)).where(InvoiceEmail.invoice_id == int(invoice_id))
                )
                or 0
            )
            rows = db.scalars(
                select(InvoiceEmail)
                .where(InvoiceEmail.invoice_id == int(invoice_id))
                .order_by(InvoiceEmail.created_at.desc(), InvoiceEmail.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
            return InvoiceEmailListResponse(
                items=[self._serialize_invoice_email(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.post(
            "/subjects/{subject_id}/invoices/{invoice_id}/public-link",
            response_model=InvoicePublicLinkModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Ensure or rotate invoice public link",
        )
        def ensure_or_rotate_invoice_public_link(
            subject_id: int,
            invoice_id: int,
            request: Request,
            payload: InvoicePublicLinkRequest = InvoicePublicLinkRequest(),
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoicePublicLinkModel | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            if bool(payload.rotate):
                invoice.public_token = generate_unique_invoice_public_token(db)
            ensure_invoice_public_link(db, invoice=invoice, subject=access.subject)
            db.add(invoice)
            db.flush()
            response_model = self._build_public_link(invoice, request=request)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.delete(
            "/subjects/{subject_id}/invoices/{invoice_id}/public-link",
            response_model=InvoicePublicLinkModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Disable invoice public link",
        )
        def disable_invoice_public_link(
            subject_id: int,
            invoice_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoicePublicLinkModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="edit")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            invoice.public_token = None
            db.add(invoice)
            db.flush()
            response_model = self._build_public_link(invoice, request=request)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/invoices/{invoice_id}/send-email",
            response_model=InvoiceSendEmailResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}, 502: {"model": ErrorEnvelopeModel}, 503: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Send invoice email",
        )
        def send_invoice_email(
            subject_id: int,
            invoice_id: int,
            payload: InvoiceSendEmailRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> InvoiceSendEmailResponse | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            if str(invoice.status or "").strip().lower() == "draft":
                raise ApiError(409, "invoice_email_requires_issued_document", "E-mail lze poslat až po vystavení dokladu.", {"invoice_id": int(invoice_id)})

            contact = self._load_contact_for_subject(db, subject_id=subject_id, contact_id=int(invoice.contact_id))
            recipients = self._validate_recipient_list(
                payload.to if payload.to is not None else self._none_str(contact.email),
                field="to",
                required=True,
                error_code="invoice_email_recipient_invalid",
                error_message="Neplatný e-mail příjemce.",
            )
            cc_recipients = self._validate_recipient_list(
                payload.cc,
                field="cc",
                required=False,
                error_code="invoice_email_cc_invalid",
                error_message="Neplatný e-mail v kopii (CC).",
            )

            from_email, from_name, smtp_cfg = self._smtp_config_for_subject(access.subject)
            if not str(smtp_cfg.host or "").strip():
                raise ApiError(503, "smtp_not_configured", "SMTP není nastavené pro odesílání z API.")
            if not looks_like_email(from_email):
                raise ApiError(503, "smtp_missing_from_email", "Chybí odesílatel (From). Nastav email subjektu nebo SMTP_FROM_EMAIL.")

            include_public_link = bool(payload.include_public_link)
            public_url: str | None = None
            if include_public_link:
                ensure_invoice_public_link(db, invoice=invoice, subject=access.subject)
                public_link = self._build_public_link(invoice, request=request)
                public_url = public_link.short_url or public_link.url
            else:
                public_link = self._build_public_link(invoice, request=request)

            email_subject = str(payload.subject or "").strip() or self._invoice_document_email_subject(invoice.document_type, invoice.number)
            body = str(payload.body or "").strip()
            if not body:
                body = self._default_invoice_email_body(invoice=invoice, public_url=public_url, from_name=from_name)
            elif public_url and public_url not in body:
                body = body.rstrip() + f"\n\nVeřejný odkaz: {public_url}\n"

            pdf_attachment: tuple[str, bytes] | None = None
            if bool(payload.attach_pdf):
                pdf_bytes = self._invoice_pdf_bytes(db, invoice=invoice)
                filename = f"{safe_filename_base(str(invoice.number or ''), fallback=f'invoice-{int(invoice.id)}')}.pdf"
                pdf_attachment = (filename, pdf_bytes)

            email_row = InvoiceEmail(
                invoice_id=int(invoice.id),
                kind="invoice",
                from_email=from_email,
                to_email=self._format_recipient_log_value(to_emails=recipients, cc_emails=cc_recipients),
                subject=email_subject[:255],
                body=body,
                status="queued",
            )
            db.add(email_row)
            db.flush()

            try:
                msg = build_email_message(
                    from_email=from_email,
                    from_name=from_name,
                    to_emails=recipients,
                    cc_emails=cc_recipients,
                    subject=email_subject,
                    body=body,
                    attachment_pdf=pdf_attachment,
                )
                message_id, _smtp_debug = send_via_smtp(smtp_cfg, msg)
                email_row.status = "sent"
                email_row.sent_at = utc_now()
                email_row.message_id = (message_id or "")[:255] if message_id else None
                email_row.error_message = None
            except Exception as exc:
                email_row.status = "error"
                email_row.sent_at = None
                logging.getLogger("fakturek").error(
                    "API invoice email failed for invoice %s (error_type=%s)",
                    invoice_id,
                    type(exc).__name__,
                )
                email_row.error_message = "E-mail se nepodařilo odeslat."
                db.add(email_row)
                db.commit()
                raise ApiError(
                    502,
                    "invoice_email_send_failed",
                    "E-mail se nepodařilo odeslat.",
                    {"invoice_id": int(invoice_id), "reason": "E-mail se nepodařilo odeslat."},
                ) from exc

            if str(invoice.status or "").strip().lower() == "issued":
                invoice.status = "sent"
                invoice.sent_at = utc_now()
            db.add(invoice)
            db.add(email_row)
            db.flush()
            response_model = InvoiceSendEmailResponse(
                email=self._serialize_invoice_email(email_row),
                invoice_status=str(invoice.status or ""),
                attached_pdf=bool(pdf_attachment is not None),
                public_link_included=bool(public_url),
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model


        @app.post(
            "/subjects/{subject_id}/invoices/bulk-action",
            response_model=BulkInvoiceActionResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["invoices"],
            summary="Apply explicit bulk workflow action to invoices",
        )
        def bulk_invoice_action(
            subject_id: int,
            payload: BulkInvoiceActionRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BulkInvoiceActionResponse | JSONResponse:
            access = self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            action = str(payload.action or "").strip().lower()
            allowed_actions = {"issue", "sent", "paid", "revert", "cancelled", "delete_draft"}
            if action not in allowed_actions:
                raise ApiError(422, "bulk_action_invalid", "Neplatná hromadná akce.", {"action": action, "allowed": sorted(allowed_actions)})
            if not payload.invoice_ids:
                raise ApiError(422, "bulk_action_empty", "Vyber alespoň jeden doklad.")
            unique_ids: list[int] = []
            seen_ids: set[int] = set()
            for raw_id in payload.invoice_ids[:200]:
                value = int(raw_id)
                if value not in seen_ids:
                    unique_ids.append(value)
                    seen_ids.add(value)

            invoice_rows = db.scalars(
                select(Invoice)
                .options(joinedload(Invoice.contact), joinedload(Invoice.subject), joinedload(Invoice.payments))
                .where(Invoice.subject_id == int(subject_id))
                .where(Invoice.id.in_(unique_ids))
            ).unique().all()
            invoices_by_id = {int(row.id): row for row in invoice_rows}

            items: list[BulkInvoiceActionItem] = []
            changed_count = 0
            deleted_count = 0
            skipped_count = 0

            for invoice_id in unique_ids:
                invoice = invoices_by_id.get(int(invoice_id))
                if invoice is None:
                    skipped_count += 1
                    items.append(BulkInvoiceActionItem(invoice_id=int(invoice_id), number=None, from_status=None, to_status=None, result="skipped", message="Doklad nebyl nalezen."))
                    continue
                old_status = str(invoice.status or "") or None
                number_before = self._none_str(invoice.number)
                try:
                    if action == "issue":
                        self._issue_invoice_draft(db, subject=access.subject, invoice=invoice)
                        changed_count += 1
                        items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=self._none_str(invoice.number), from_status=old_status, to_status=str(invoice.status or ""), result="changed"))
                        continue
                    if action == "delete_draft":
                        if str(invoice.status or "").strip().lower() != "draft":
                            skipped_count += 1
                            items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=number_before, from_status=old_status, to_status=old_status, result="skipped", message="Smazat lze jen koncept."))
                            continue
                        db.delete(invoice)
                        db.flush()
                        deleted_count += 1
                        items.append(BulkInvoiceActionItem(invoice_id=int(invoice_id), number=number_before, from_status=old_status, to_status=None, result="deleted"))
                        continue
                    target = action
                    if action == "revert":
                        target = self._invoice_revert_target(invoice) or ""
                        if not target:
                            skipped_count += 1
                            items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=number_before, from_status=old_status, to_status=old_status, result="skipped", message="Doklad nejde vrátit o krok zpět."))
                            continue
                    changed, error = self._apply_invoice_status_transition(invoice, new_status=target)
                    if not changed:
                        skipped_count += 1
                        items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=number_before, from_status=old_status, to_status=old_status, result="skipped", message=error))
                        continue
                    if action == "paid" and payload.paid_on is not None:
                        invoice.paid_on = payload.paid_on
                    if action == "paid":
                        self._ensure_manual_invoice_payment(
                            db,
                            invoice=invoice,
                            paid_on=getattr(invoice, "paid_on", None),
                            source="api_bulk",
                        )
                    elif str(old_status or "").strip().lower() == "paid" and target in {"issued", "sent"}:
                        self._remove_unlinked_manual_invoice_payments(db, invoice=invoice)
                    db.add(invoice)
                    db.flush()
                    changed_count += 1
                    items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=self._none_str(invoice.number), from_status=old_status, to_status=str(invoice.status or ""), result="changed"))
                except ApiError as exc:
                    skipped_count += 1
                    items.append(BulkInvoiceActionItem(invoice_id=int(invoice.id), number=number_before, from_status=old_status, to_status=old_status, result="skipped", message=exc.message))

            response_model = BulkInvoiceActionResponse(
                action=action,
                requested_count=len(unique_ids),
                changed_count=changed_count,
                skipped_count=skipped_count,
                deleted_count=deleted_count,
                items=items,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/invoices/{invoice_id}/payments",
            response_model=PaymentListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["payments"],
            summary="List invoice payments",
        )
        def list_invoice_payments(
            subject_id: int,
            invoice_id: int,
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> PaymentListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            total = int(db.scalar(select(func.count(Payment.id)).where(Payment.invoice_id == int(invoice.id))) or 0)
            rows = db.scalars(
                select(Payment)
                .options(joinedload(Payment.bank_transactions))
                .where(Payment.invoice_id == int(invoice.id))
                .order_by(Payment.paid_on.desc(), Payment.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).unique().all()
            return PaymentListResponse(
                items=[self._serialize_payment(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.post(
            "/subjects/{subject_id}/invoices/{invoice_id}/payments",
            response_model=PaymentModel,
            status_code=201,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["payments"],
            summary="Create manual payment or match imported transaction",
        )
        def create_invoice_payment(
            subject_id: int,
            invoice_id: int,
            payload: PaymentCreateRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> PaymentModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            status = str(invoice.status or "").strip().lower()
            if status not in {"issued", "sent", "paid"}:
                raise ApiError(409, "invoice_payment_state_invalid", "Platbu lze přidat jen k vystavenému, odeslanému nebo zaplacenému dokladu.", {"invoice_id": int(invoice.id), "status": status})

            if payload.bank_transaction_id is not None:
                row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=int(payload.bank_transaction_id))
                payment = self._link_bank_transaction_to_invoice(db, row=row, invoice=invoice, note=payload.note)
            else:
                amount_cents = int(invoice.total_cents or 0) if payload.amount is None else parse_money_to_cents(payload.amount)
                if amount_cents <= 0:
                    raise ApiError(422, "payment_amount_invalid", "Částka platby musí být kladná.")
                note = self._validate_payment_note(payload.note)
                payment = Payment(
                    invoice_id=int(invoice.id),
                    paid_on=payload.paid_on or date.today(),
                    amount_cents=amount_cents,
                    note=note,
                )
                db.add(payment)
                db.flush()
                if status != "paid":
                    changed, error = self._apply_invoice_status_transition(invoice, new_status="paid")
                    if not changed and str(invoice.status or "").strip().lower() != "paid":
                        raise ApiError(409, "invoice_payment_transition_invalid", error or "Nepodařilo se označit doklad jako zaplacený.", {"invoice_id": int(invoice.id), "status": status})
                self._refresh_invoice_payment_state(db, invoice=invoice)
                db.add(invoice)
                db.flush()
                payment = self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=int(payment.id))

            response_model = self._serialize_payment(payment)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=201,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}",
            response_model=PaymentModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["payments"],
            summary="Get payment detail",
        )
        def get_invoice_payment(
            subject_id: int,
            invoice_id: int,
            payment_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> PaymentModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            payment = self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=payment_id)
            return self._serialize_payment(payment)

        @app.patch(
            "/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}",
            response_model=PaymentModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["payments"],
            summary="Patch payment metadata",
        )
        def patch_invoice_payment(
            subject_id: int,
            invoice_id: int,
            payment_id: int,
            payload: PaymentPatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> PaymentModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            payment = self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=payment_id)
            fields_set = payload.model_fields_set
            has_bank_links = bool(list(getattr(payment, "bank_transactions", []) or []))
            if has_bank_links and ({"paid_on", "amount"} & set(fields_set)):
                raise ApiError(409, "payment_linked_to_bank_transaction", "Částku ani datum nejde měnit u platby navázané na bankovní transakci.", {"payment_id": int(payment.id)})
            if "paid_on" in fields_set and payload.paid_on is not None:
                payment.paid_on = payload.paid_on
            if "amount" in fields_set and payload.amount is not None:
                amount_cents = parse_money_to_cents(payload.amount)
                if amount_cents <= 0:
                    raise ApiError(422, "payment_amount_invalid", "Částka platby musí být kladná.")
                payment.amount_cents = amount_cents
            if "note" in fields_set:
                payment.note = self._validate_payment_note(payload.note)
            db.add(payment)
            self._refresh_invoice_payment_state(db, invoice=invoice)
            db.add(invoice)
            db.flush()
            response_model = self._serialize_payment(self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=int(payment.id)))
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=True),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.delete(
            "/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}",
            response_model=DeleteResultModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["payments"],
            summary="Delete payment and detach linked bank transaction",
        )
        def delete_invoice_payment(
            subject_id: int,
            invoice_id: int,
            payment_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> DeleteResultModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay

            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
            payment = self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=payment_id)
            for row in list(getattr(payment, "bank_transactions", []) or []):
                row.matched_invoice_id = None
                row.payment_id = None
                row.matched_at = None
                db.add(row)
            try:
                if payment in list(getattr(invoice, "payments", []) or []):
                    invoice.payments.remove(payment)
            except Exception:
                pass
            db.delete(payment)
            db.flush()
            self._refresh_invoice_payment_state(db, invoice=invoice)
            response_model = DeleteResultModel(deleted=True, id=int(payment_id))
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/bank-transactions",
            response_model=BankTransactionListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-transactions"],
            summary="List imported bank transactions",
        )
        def list_bank_transactions(
            subject_id: int,
            bank_account_id: int | None = Query(None),
            matched: bool | None = Query(None),
            direction: str | None = Query(None),
            provider: str | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankTransactionListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            stmt = (
                select(BankTransaction)
                .options(
                    joinedload(BankTransaction.bank_account),
                    joinedload(BankTransaction.matched_invoice),
                    joinedload(BankTransaction.payment),
                )
                .join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id)
                .where(SubjectBankAccount.subject_id == int(subject_id))
                .order_by(BankTransaction.booked_on.desc(), BankTransaction.id.desc())
            )
            count_stmt = select(func.count(BankTransaction.id)).join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id).where(SubjectBankAccount.subject_id == int(subject_id))
            if bank_account_id is not None:
                stmt = stmt.where(BankTransaction.subject_bank_account_id == int(bank_account_id))
                count_stmt = count_stmt.where(BankTransaction.subject_bank_account_id == int(bank_account_id))
            if matched is True:
                stmt = stmt.where(BankTransaction.matched_invoice_id.is_not(None))
                count_stmt = count_stmt.where(BankTransaction.matched_invoice_id.is_not(None))
            elif matched is False:
                stmt = stmt.where(BankTransaction.matched_invoice_id.is_(None))
                count_stmt = count_stmt.where(BankTransaction.matched_invoice_id.is_(None))
            if direction:
                stmt = stmt.where(BankTransaction.direction == str(direction).strip())
                count_stmt = count_stmt.where(BankTransaction.direction == str(direction).strip())
            if provider:
                stmt = stmt.where(BankTransaction.provider == str(provider).strip())
                count_stmt = count_stmt.where(BankTransaction.provider == str(provider).strip())
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).unique().all()
            return BankTransactionListResponse(
                items=[self._serialize_bank_transaction(row) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}/bank-transactions/{transaction_id}",
            response_model=BankTransactionModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-transactions"],
            summary="Get bank transaction detail",
        )
        def get_bank_transaction(
            subject_id: int,
            transaction_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankTransactionModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=transaction_id)
            return self._serialize_bank_transaction(row)

        @app.post(
            "/subjects/{subject_id}/bank-transactions/{transaction_id}/match",
            response_model=BankTransactionMatchResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 409: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["bank-transactions"],
            summary="Manually match imported bank transaction to invoice",
        )
        def match_bank_transaction(
            subject_id: int,
            transaction_id: int,
            payload: BankTransactionMatchRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankTransactionMatchResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay
            row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=transaction_id)
            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(payload.invoice_id))
            payment = self._link_bank_transaction_to_invoice(db, row=row, invoice=invoice, note=payload.note)
            row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=transaction_id)
            invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=int(invoice.id))
            response_model = BankTransactionMatchResponse(
                transaction=self._serialize_bank_transaction(row),
                payment=self._serialize_payment(payment),
                invoice_status=str(invoice.status or ""),
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-transactions/{transaction_id}/unmatch",
            response_model=BankTransactionUnmatchResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-transactions"],
            summary="Detach bank transaction from invoice/payment",
        )
        def unmatch_bank_transaction(
            subject_id: int,
            transaction_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankTransactionUnmatchResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay
            row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=transaction_id)
            deleted_payment_id, invoice = self._unlink_bank_transaction(db, row=row)
            row = self._load_bank_transaction_for_subject(db, subject_id=subject_id, transaction_id=transaction_id)
            response_model = BankTransactionUnmatchResponse(
                transaction=self._serialize_bank_transaction(row),
                deleted_payment_id=deleted_payment_id,
                invoice_status=str(getattr(invoice, "status", "") or "") if invoice is not None else "",
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-accounts/{bank_account_id}/retry-matching",
            response_model=RetryBankTransactionMatchResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-transactions"],
            summary="Retry matching existing unmatched imported transactions",
        )
        def retry_bank_transaction_matching(
            subject_id: int,
            bank_account_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> RetryBankTransactionMatchResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay
            account = self._load_bank_account_for_subject(db, subject_id=subject_id, bank_account_id=bank_account_id)
            inspected, matched = self._retry_existing_unmatched_bank_transactions(db, account=account)
            remaining_unmatched = int(
                db.scalar(
                    select(func.count(BankTransaction.id))
                    .where(BankTransaction.subject_bank_account_id == int(account.id))
                    .where(BankTransaction.matched_invoice_id.is_(None))
                    .where(BankTransaction.direction == "incoming")
                    .where(BankTransaction.amount_cents > 0)
                )
                or 0
            )
            response_model = RetryBankTransactionMatchResponse(
                bank_account_id=int(account.id),
                inspected=inspected,
                matched=matched,
                remaining_unmatched=remaining_unmatched,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-sync/run",
            response_model=BankSyncRunResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Run bank sync for all subject bank accounts",
        )
        def run_subject_bank_sync(
            subject_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankSyncRunResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay
            response_model = self._run_bank_sync_for_subject(db, subject_id=subject_id)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-accounts/{bank_account_id}/sync",
            response_model=BankSyncRunAccountModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Run bank sync for a single bank account",
        )
        def run_bank_account_sync(
            subject_id: int,
            bank_account_id: int,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankSyncRunAccountModel | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
            )
            if replay is not None:
                return replay
            account = self._load_bank_account_for_subject(db, subject_id=subject_id, bank_account_id=bank_account_id)
            response_model = self._sync_subject_bank_account(db, account=account)
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash({}, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-transactions",
            response_model=BankTransactionImportResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Import normalized bank transactions via API",
        )
        def import_bank_transactions(
            subject_id: int,
            bank_account_id: int,
            payload: BankTransactionImportRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankTransactionImportResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay
            account = self._load_bank_account_for_subject(db, subject_id=subject_id, bank_account_id=bank_account_id)
            items = list(payload.items or [])
            if not items:
                raise ApiError(422, "bank_transaction_import_empty", "Import musí obsahovat alespoň jednu položku.")
            if len(items) > 100:
                raise ApiError(422, "bank_transaction_import_too_large", "Jedním requestem lze importovat maximálně 100 transakcí.")

            auto_pair = bool(payload.auto_pair) if payload.auto_pair is not None else bool(getattr(account, "payment_sync_auto_pair", True))
            imported_count = 0
            matched_count = 0
            skipped_existing_count = 0
            results: list[BankTransactionImportItemResult] = []

            for item in items:
                try:
                    imported = self._manual_imported_bank_transaction(item, account=account)
                    row, was_imported, matched = self._import_bank_transaction_row(
                        db,
                        account=account,
                        imported=imported,
                        auto_pair=auto_pair,
                    )
                    if was_imported:
                        imported_count += 1
                    else:
                        skipped_existing_count += 1
                    if matched:
                        matched_count += 1
                    results.append(
                        BankTransactionImportItemResult(
                            external_id=imported.external_id,
                            result="imported" if was_imported else "skipped_existing",
                            matched=matched,
                            transaction=self._serialize_bank_transaction(row),
                            message=None,
                        )
                    )
                except ApiError as exc:
                    results.append(
                        BankTransactionImportItemResult(
                            external_id=str(getattr(item, "external_id", "") or ""),
                            result="error",
                            matched=False,
                            transaction=None,
                            message=str(exc.message),
                        )
                    )
                except Exception:
                    logging.getLogger("fakturek").exception(
                        "Unexpected bank transaction import failure for subject_id=%s bank_account_id=%s",
                        subject_id,
                        bank_account_id,
                    )
                    results.append(
                        BankTransactionImportItemResult(
                            external_id=str(getattr(item, "external_id", "") or ""),
                            result="error",
                            matched=False,
                            transaction=None,
                            message="Import transakce selhal.",
                        )
                    )

            response_model = BankTransactionImportResponse(
                bank_account_id=int(account.id),
                requested_count=len(items),
                imported_count=imported_count,
                matched_count=matched_count,
                skipped_existing_count=skipped_existing_count,
                items=results,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.get(
            "/subjects/{subject_id}/bank-incoming-emails",
            response_model=BankIncomingEmailListResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="List stored bank notification emails",
        )
        def list_bank_incoming_emails(
            subject_id: int,
            bank_account_id: int | None = Query(None),
            processing_status: str | None = Query(None),
            page: int = Query(1, ge=1),
            per_page: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankIncomingEmailListResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            stmt = (
                select(BankIncomingEmail)
                .options(joinedload(BankIncomingEmail.bank_account), joinedload(BankIncomingEmail.matched_bank_transaction))
                .join(SubjectBankAccount, SubjectBankAccount.id == BankIncomingEmail.subject_bank_account_id)
                .where(SubjectBankAccount.subject_id == int(subject_id))
                .order_by(BankIncomingEmail.received_at.desc(), BankIncomingEmail.id.desc())
            )
            count_stmt = select(func.count(BankIncomingEmail.id)).join(SubjectBankAccount, SubjectBankAccount.id == BankIncomingEmail.subject_bank_account_id).where(SubjectBankAccount.subject_id == int(subject_id))
            if bank_account_id is not None:
                stmt = stmt.where(BankIncomingEmail.subject_bank_account_id == int(bank_account_id))
                count_stmt = count_stmt.where(BankIncomingEmail.subject_bank_account_id == int(bank_account_id))
            if processing_status:
                stmt = stmt.where(BankIncomingEmail.processing_status == str(processing_status).strip())
                count_stmt = count_stmt.where(BankIncomingEmail.processing_status == str(processing_status).strip())
            total = int(db.scalar(count_stmt) or 0)
            rows = db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).unique().all()
            return BankIncomingEmailListResponse(
                items=[self._serialize_bank_incoming_email(row, preview_limit=160) for row in rows],
                page=page,
                per_page=per_page,
                total_items=total,
                total_pages=max(1, ceil(total / per_page)) if total else 1,
            )

        @app.get(
            "/subjects/{subject_id}/bank-incoming-emails/{email_id}",
            response_model=BankIncomingEmailModel,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Get stored bank notification email detail",
        )
        def get_bank_incoming_email(
            subject_id: int,
            email_id: int,
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankIncomingEmailModel:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="view")
            row = self._load_bank_incoming_email_for_subject(db, subject_id=subject_id, email_id=email_id)
            return self._serialize_bank_incoming_email(row, preview_limit=500)

        @app.post(
            "/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-email",
            response_model=BankIncomingEmailImportResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Store and optionally parse a bank notification email",
        )
        def import_bank_incoming_email(
            subject_id: int,
            bank_account_id: int,
            payload: BankIncomingEmailImportRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankIncomingEmailImportResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay
            account = self._load_bank_account_for_subject(db, subject_id=subject_id, bank_account_id=bank_account_id)
            email_row = self._store_api_bank_incoming_email(db, account=account, payload=payload)
            parser_name = payload.parser or getattr(account, "payment_sync_email_parser", None)
            auto_pair = bool(payload.auto_pair) if payload.auto_pair is not None else bool(getattr(account, "payment_sync_auto_pair", True))
            _outcome, matched, transaction = self._process_bank_incoming_email_row(
                db,
                account=account,
                email_row=email_row,
                auto_pair=auto_pair,
                parser_name=parser_name,
            )
            response_model = BankIncomingEmailImportResponse(
                email=self._serialize_bank_incoming_email(email_row, preview_limit=500),
                transaction=self._serialize_bank_transaction(transaction) if transaction is not None else None,
                matched=matched,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

        @app.post(
            "/subjects/{subject_id}/bank-incoming-emails/{email_id}/reprocess",
            response_model=BankIncomingEmailImportResponse,
            responses={401: {"model": ErrorEnvelopeModel}, 403: {"model": ErrorEnvelopeModel}, 404: {"model": ErrorEnvelopeModel}, 422: {"model": ErrorEnvelopeModel}},
            tags=["bank-sync"],
            summary="Reprocess a stored bank notification email",
        )
        def reprocess_bank_incoming_email(
            subject_id: int,
            email_id: int,
            payload: BankIncomingEmailReprocessRequest,
            request: Request,
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
            db: Session = Depends(get_db),
            actor: ApiActor = Depends(self._require_api_actor),
        ) -> BankIncomingEmailImportResponse | JSONResponse:
            self._require_subject_access(db, actor=actor, subject_id=subject_id, permission="issue")
            replay = self._replay_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
            )
            if replay is not None:
                return replay
            email_row = self._load_bank_incoming_email_for_subject(db, subject_id=subject_id, email_id=email_id)
            account = self._load_bank_account_for_subject(
                db,
                subject_id=subject_id,
                bank_account_id=int(email_row.subject_bank_account_id),
            )
            parser_name = payload.parser or getattr(account, "payment_sync_email_parser", None)
            auto_pair = bool(payload.auto_pair) if payload.auto_pair is not None else bool(getattr(account, "payment_sync_auto_pair", True))
            _outcome, matched, transaction = self._process_bank_incoming_email_row(
                db,
                account=account,
                email_row=email_row,
                auto_pair=auto_pair,
                parser_name=parser_name,
            )
            response_model = BankIncomingEmailImportResponse(
                email=self._serialize_bank_incoming_email(email_row, preview_limit=500),
                transaction=self._serialize_bank_transaction(transaction) if transaction is not None else None,
                matched=matched,
            )
            self._remember_idempotent_response(
                db,
                actor=actor,
                request=request,
                idempotency_key=idempotency_key,
                request_hash=self._request_hash(payload, exclude_unset=False),
                response_status=200,
                response_body=response_model.model_dump(),
                subject_id=int(subject_id),
            )
            db.commit()
            return response_model

    def _require_api_actor(
        self,
        db: Session = Depends(get_db),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> ApiActor:
        if credentials is None or str(credentials.scheme or "").lower() != "bearer":
            raise ApiError(401, "auth_missing_bearer", "Chybí Bearer token.")
        token_value = str(credentials.credentials or "").strip()
        if not token_value:
            raise ApiError(401, "auth_invalid_token", "Bearer token je prázdný.")
        token_hash = hash_api_token_value(token_value)
        row = db.execute(
            select(ApiToken, User)
            .join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_hash == token_hash)
            .limit(1)
        ).first()
        if row is None:
            raise ApiError(401, "auth_invalid_token", "Bearer token je neplatný.")
        token, user = row
        now = utc_now()
        if token.revoked_at is not None:
            raise ApiError(401, "auth_revoked_token", "Bearer token byl odvolán.")
        token_expires_at = as_utc_aware(token.expires_at)
        if token_expires_at is not None and token_expires_at <= now:
            raise ApiError(401, "auth_expired_token", "Bearer token expiroval.")
        if not bool(user.is_active):
            raise ApiError(403, "auth_user_inactive", "Uživatel je neaktivní.")

        scoped_subject_id = getattr(token, "subject_id", None)
        if scoped_subject_id is None:
            raise ApiError(
                401,
                "auth_token_scope_missing",
                "Bearer token nemá přiřazený konkrétní subjekt. Vytvořte prosím nový token pro vybrané IČO.",
                {"token_id": int(token.id)},
            )
        scoped_subject = db.scalar(
            select(Subject)
            .where(Subject.id == int(scoped_subject_id))
            .limit(1)
        )
        if scoped_subject is None:
            raise ApiError(
                401,
                "auth_token_scope_invalid",
                "Bearer token odkazuje na neexistující subjekt.",
                {"token_id": int(token.id), "subject_id": int(scoped_subject_id)},
            )
        scoped_link = db.scalar(
            select(UserSubject)
            .where(UserSubject.user_id == int(user.id))
            .where(UserSubject.subject_id == int(scoped_subject_id))
            .limit(1)
        )
        if scoped_link is None or not bool(getattr(scoped_link, "can_view", False)):
            raise ApiError(
                403,
                "auth_token_subject_access_revoked",
                "Bearer token už nemá přístup k přiřazenému subjektu.",
                {"token_id": int(token.id), "subject_id": int(scoped_subject_id)},
            )

        self._rate_limit_api_token_or_429(db=db, token=token)

        last_used_at = as_utc_aware(token.last_used_at)
        if last_used_at is None or (now - last_used_at).total_seconds() >= 60:
            token.last_used_at = now
            db.add(token)
            db.commit()
            db.refresh(token)

        return ApiActor(user=user, token=token, subject=scoped_subject, link=scoped_link)

    def _require_subject_access(self, db: Session, *, actor: ApiActor, subject_id: int, permission: str, sandbox: bool = False) -> SubjectAccess:
        requested_subject_id = int(subject_id)
        scoped_subject_id = int(actor.subject.id)
        if requested_subject_id != scoped_subject_id:
            raise ApiError(
                403,
                "subject_access_denied",
                "Bearer token je omezený na jiný subjekt.",
                {
                    "requested_subject_id": requested_subject_id,
                    "token_subject_id": scoped_subject_id,
                    "token_scope": True,
                },
            )
        link = actor.link
        if permission == "view" and not bool(link.can_view):
            raise ApiError(403, "subject_access_denied", "K tomuto subjektu nemáte přístup.", {"subject_id": requested_subject_id, "required": "can_view"})
        if permission == "edit" and not bool(link.can_edit):
            raise ApiError(403, "subject_access_denied", "K tomuto subjektu nemáte právo zapisovat.", {"subject_id": requested_subject_id, "required": "can_edit"})
        if permission == "issue" and not bool(link.can_issue):
            raise ApiError(403, "subject_access_denied", "K tomuto subjektu nemáte právo vystavovat.", {"subject_id": requested_subject_id, "required": "can_issue"})
        if permission == "export" and not bool(getattr(link, "can_export", False)):
            raise ApiError(403, "subject_access_denied", "K tomuto subjektu nemáte právo exportovat.", {"subject_id": requested_subject_id, "required": "can_export"})
        token_scope_map = {"view": "can_read", "edit": "can_write", "issue": "can_issue", "export": "can_export"}
        token_attr = token_scope_map.get(permission, "can_read")
        if not bool(getattr(actor.token, token_attr, False)):
            raise ApiError(403, "api_token_scope_denied", "API klíč nemá oprávnění pro tuhle akci.", {"subject_id": requested_subject_id, "required": token_attr})
        if bool(getattr(actor.token, "is_sandbox", False)) and permission in {"edit", "issue", "export"} and not sandbox:
            raise ApiError(
                403,
                "api_token_sandbox_only",
                "Tenhle API klíč je ve zkušebním režimu a nesmí měnit ostrá data. Použij sandbox endpoint.",
                {"subject_id": requested_subject_id, "sandbox_endpoint": f"/api/v1/subjects/{requested_subject_id}/sandbox/invoices"},
            )

        return SubjectAccess(subject=actor.subject, link=link)

    def _api_monthly_quota_period(self) -> tuple[int, int, date, date, int]:
        now_local = datetime.now(API_MONTHLY_QUOTA_TZ)
        year = int(now_local.year)
        month = int(now_local.month)
        month_start = date(year, month, 1)
        if month == 12:
            next_month_start = date(year + 1, 1, 1)
        else:
            next_month_start = date(year, month + 1, 1)
        retry_after = max(1, int((datetime.combine(next_month_start, datetime.min.time(), tzinfo=API_MONTHLY_QUOTA_TZ) - now_local).total_seconds()))
        return year, month, month_start, next_month_start, retry_after

    def _consume_api_monthly_quota_or_429(self, db: Session, *, token: ApiToken) -> None:
        limit = max(1, int(getattr(self.settings, "api_monthly_quota_max", 2500) or 2500))
        year, month, month_start, next_month_start, retry_after = self._api_monthly_quota_period()
        usage_row = db.scalar(
            select(ApiTokenMonthlyUsage)
            .where(ApiTokenMonthlyUsage.token_id == int(token.id))
            .where(ApiTokenMonthlyUsage.usage_year == year)
            .where(ApiTokenMonthlyUsage.usage_month == month)
            .limit(1)
        )
        now_utc = utc_now()
        if usage_row is None:
            usage_row = ApiTokenMonthlyUsage(
                token_id=int(token.id),
                usage_year=year,
                usage_month=month,
                request_count=0,
                window_started_at=now_utc,
                last_request_at=None,
            )
            db.add(usage_row)
            db.flush()
        used = int(getattr(usage_row, "request_count", 0) or 0)
        period_label = f"{year}-{month:02d}"
        if used >= limit:
            raise ApiError(
                429,
                "api_monthly_quota_exceeded",
                "Měsíční API limit byl vyčerpán. Zkus to prosím znovu příští měsíc.",
                {
                    "token_id": int(token.id),
                    "monthly_limit": limit,
                    "monthly_used": used,
                    "monthly_remaining": 0,
                    "period": period_label,
                    "period_start": month_start.isoformat(),
                    "period_end_exclusive": next_month_start.isoformat(),
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Monthly-Limit": str(limit),
                    "X-RateLimit-Monthly-Remaining": "0",
                    "X-RateLimit-Monthly-Period": period_label,
                },
            )
        usage_row.request_count = used + 1
        usage_row.last_request_at = now_utc
        db.add(usage_row)
        db.commit()

    def _rate_limit_api_token_or_429(self, db: Session, *, token: ApiToken) -> None:
        decision = self.api_rate_limiter.check(f"api-token:{int(token.id)}")
        if decision.allowed:
            self._consume_api_monthly_quota_or_429(db, token=token)
            return
        raise ApiError(
            429,
            "api_rate_limited",
            "API rate limit byl překročen. Zkuste to prosím za chvíli znovu.",
            {
                "token_id": int(token.id),
                "limit": int(self.api_rate_limiter.max_requests),
                "window_seconds": int(self.api_rate_limiter.window_seconds),
                "remaining": int(decision.remaining),
            },
            headers={
                "Retry-After": str(decision.retry_after),
                "X-RateLimit-Limit": str(int(self.api_rate_limiter.max_requests)),
                "X-RateLimit-Remaining": str(int(decision.remaining)),
                "X-RateLimit-Window": str(int(self.api_rate_limiter.window_seconds)),
            },
        )

    def _list_subjects_for_actor(self, actor: ApiActor) -> list[SubjectSummaryModel]:
        return [self._serialize_subject(actor.subject, actor.link)]

    def _serialize_subject(self, subject: Subject, link: UserSubject) -> SubjectSummaryModel:
        return SubjectSummaryModel(
            id=int(subject.id),
            name=str(subject.name or ""),
            public_username=(str(subject.public_username or "").strip() or None),
            email=str(subject.email or ""),
            phone=str(subject.phone or ""),
            ico=str(subject.ico or ""),
            dic=str(subject.dic or ""),
            country=str(subject.country or ""),
            city=str(subject.city or ""),
            zip=str(subject.zip or ""),
            street=str(subject.street or ""),
            default_currency=str(subject.default_currency or "CZK"),
            is_vat_payer=bool(subject.is_vat_payer),
            tax_regime=str(subject.tax_regime or "standard"),
            permissions=SubjectPermissionsModel(
                role=str(link.role or ""),
                can_view=bool(link.can_view),
                can_edit=bool(link.can_edit),
                can_issue=bool(link.can_issue),
                can_export=bool(getattr(link, "can_export", False)),
            ),
        )

    def _serialize_invoice_series(
        self,
        db: Session,
        *,
        subject_id: int,
        row: InvoiceSeries,
        year: int | None = None,
    ) -> InvoiceSeriesModel:
        number_year = int(year or self._invoice_number_year())
        next_number_preview = self._invoice_series_next_number_preview(db, subject_id=subject_id, series=row, year=number_year)
        return InvoiceSeriesModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            name=str(row.name or ""),
            prefix=str(row.prefix or ""),
            pad_length=int(row.pad_length or 0),
            last_counter=int(row.last_counter or 0),
            last_counter_year=(int(row.last_counter_year) if row.last_counter_year is not None else None),
            next_number_preview=next_number_preview,
        )

    def _serialize_bank_account(self, row: SubjectBankAccount) -> BankAccountModel:
        iban = self._none_str(row.iban)
        iban_display: str | None
        if iban:
            try:
                iban_display = format_iban_for_display(iban)
            except ValueError:
                iban_display = iban
        else:
            iban_display = None
        country = str(row.country or "")
        account_number = "" if country.upper() == "SK" else self._none_str(row.account_number)
        display_account = account_number or iban_display or str(row.label or "")
        return BankAccountModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            label=str(row.label or ""),
            account_number=account_number,
            iban=iban,
            iban_display=iban_display,
            bic=self._none_str(row.bic),
            country=country,
            currency=str(row.currency or "CZK"),
            display_account=display_account,
            is_default=bool(row.is_default),
            sort_order=int(row.sort_order or 0),
            payment_sync_provider=str(row.payment_sync_provider or "none"),
            payment_sync_enabled=bool(row.payment_sync_enabled),
            payment_sync_auto_pair=bool(row.payment_sync_auto_pair),
            payment_sync_last_checked_at=self._dt(row.payment_sync_last_checked_at),
            payment_sync_last_success_at=self._dt(row.payment_sync_last_success_at),
            payment_sync_last_error=(
                safe_bank_sync_error_message(row.payment_sync_last_error)
                if self._none_str(row.payment_sync_last_error)
                else None
            ),
        )

    def _serialize_catalog_item(self, row: InvoiceCatalogItem) -> CatalogItemModel:
        return CatalogItemModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            description=str(row.description or ""),
            quantity=self._decimal(row.quantity),
            unit=str(row.unit or ""),
            unit_price=self._money(row.unit_price_cents),
            vat_rate=self._decimal(row.vat_rate),
            currency=str(row.currency or "CZK"),
            created_at=self._dt(row.created_at),
            updated_at=self._dt(row.updated_at),
        )

    def _serialize_contact(self, row: Contact) -> ContactModel:
        return ContactModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            name=str(row.name or ""),
            email=self._none_str(row.email),
            phone=self._none_str(row.phone),
            street=self._none_str(row.street),
            city=self._none_str(row.city),
            zip=self._none_str(row.zip),
            country=self._none_str(row.country),
            ico=self._none_str(row.ico),
            dic=self._none_str(row.dic),
            fixed_variable_symbol=self._none_str(row.fixed_variable_symbol),
            external_source=self._none_str(row.external_source),
            external_id=self._none_str(row.external_id),
            created_at=self._dt(row.created_at),
            updated_at=self._dt(row.updated_at),
        )

    def _serialize_invoice_ref(self, row: Invoice | None) -> InvoiceRefModel | None:
        if row is None:
            return None
        return InvoiceRefModel(
            id=int(row.id),
            number=str(row.number or ""),
            document_type=str(row.document_type or "invoice"),
            status=str(row.status or "draft"),
        )

    def _serialize_recurring_plan(self, db: Session, row: RecurringInvoicePlan) -> RecurringPlanModel:
        template_invoice = getattr(row, "template_invoice", None)
        if template_invoice is None and getattr(row, "template_invoice_id", None) is not None:
            template_invoice = db.get(Invoice, int(row.template_invoice_id))
        last_generated_invoice = None
        if getattr(row, "last_generated_invoice_id", None) is not None:
            last_generated_invoice = db.get(Invoice, int(row.last_generated_invoice_id))
        template_ref = self._serialize_invoice_ref(template_invoice)
        if template_ref is None:
            raise ApiError(409, "recurring_template_not_found", "Šablona opakování není dostupná.", {"plan_id": int(row.id)})
        return RecurringPlanModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            name=str(row.name or ""),
            template_invoice=template_ref,
            interval_unit=str(row.interval_unit or "month"),
            interval_count=int(row.interval_count or 1),
            next_issue_date=self._d(row.next_issue_date) or date.today().isoformat(),
            due_in_days=int(row.due_in_days or 14),
            is_active=bool(row.is_active),
            auto_issue=bool(row.auto_issue),
            auto_send=bool(row.auto_send),
            email_override=self._none_str(row.email_override),
            last_run_at=self._dt(row.last_run_at),
            last_generated_invoice=self._serialize_invoice_ref(last_generated_invoice),
            created_at=self._dt(row.created_at),
            updated_at=self._dt(row.updated_at),
        )

    def _serialize_invoice_summary(self, row: Invoice, *, request: Request | None) -> InvoiceSummaryModel:
        public_link = self._build_public_link(row, request=request)
        buyer_name = str((getattr(row.contact, "name", None) or row.buyer_name_cache or "").strip())
        return InvoiceSummaryModel(
            id=int(row.id),
            subject_id=int(row.subject_id),
            number=str(row.number or ""),
            status=str(row.status or "draft"),
            document_type=str(row.document_type or "invoice"),
            issue_date=self._d(row.issue_date),
            due_date=self._d(row.due_date),
            currency=str(row.currency or "CZK"),
            total=self._money(row.total_cents),
            discount=self._money(row.discount_cents),
            rounding_adjustment=self._money(row.rounding_adjustment_cents),
            variable_symbol=self._none_str(row.variable_symbol),
            payment_method=str(row.payment_method or ""),
            invoice_language=str(row.invoice_language or "cs"),
            invoice_style=self._coerce_invoice_style(getattr(row, "invoice_style", None)),
            issued_at=self._dt(row.issued_at),
            sent_at=self._dt(row.sent_at),
            paid_on=self._d(row.paid_on),
            pdf_available=bool((row.pdf_path or "").strip()),
            contact=InvoiceContactRefModel(
                id=(int(row.contact_id) if row.contact_id is not None else None),
                name=buyer_name,
            ),
            public_link=public_link,
        )

    def _serialize_invoice_detail(self, row: Invoice, *, request: Request | None) -> InvoiceDetailModel:
        base = self._serialize_invoice_summary(row, request=request)
        return InvoiceDetailModel(
            **base.model_dump(),
            notes=self._none_str(row.notes),
            internal_notes=self._none_str(row.internal_notes),
            source_invoice_id=(int(row.source_invoice_id) if row.source_invoice_id is not None else None),
            series_id=(int(row.series_id) if row.series_id is not None else None),
            footer_mode=str(row.footer_mode or ""),
            footer_text=self._none_str(row.footer_text),
            bank_account_id=(int(row.bank_account_id) if row.bank_account_id is not None else None),
            bank_account_label=self._none_str(row.bank_account_label),
            bank_account_number=self._none_str(row.bank_account_number),
            bank_account_iban=self._none_str(row.bank_account_iban),
            bank_account_bic=self._none_str(row.bank_account_bic),
            bank_account_country=self._none_str(row.bank_account_country),
            items=[self._serialize_invoice_item(item) for item in sorted(row.items, key=lambda x: (x.sort_order, x.id))],
            parties=[self._serialize_invoice_party(party) for party in sorted(row.parties, key=lambda x: (x.role, x.id))],
            payments=[self._serialize_payment(payment) for payment in sorted(row.payments, key=lambda x: (x.paid_on, x.id))],
            created_at=self._dt(row.created_at),
            updated_at=self._dt(row.updated_at),
        )

    def _serialize_invoice_item(self, row: InvoiceItem) -> InvoiceItemModel:
        return InvoiceItemModel(
            id=int(row.id),
            description=str(row.description or ""),
            quantity=self._decimal(row.quantity),
            unit=str(row.unit or ""),
            unit_price=self._money(row.unit_price_cents),
            vat_rate=self._decimal(row.vat_rate),
            line_net=self._money(row.line_net_cents),
            line_vat=self._money(row.line_vat_cents),
            line_total=self._money(row.line_total_cents),
            sort_order=int(row.sort_order or 0),
        )

    def _serialize_invoice_party(self, row: InvoiceParty) -> InvoicePartyModel:
        return InvoicePartyModel(
            role=str(row.role or ""),
            name=str(row.name or ""),
            email=str(row.email or ""),
            phone=str(row.phone or ""),
            street=str(row.street or ""),
            city=str(row.city or ""),
            zip=str(row.zip or ""),
            country=str(row.country or ""),
            ico=str(row.ico or ""),
            dic=str(row.dic or ""),
        )

    def _serialize_payment(self, row: Payment) -> PaymentModel:
        return PaymentModel(
            id=int(row.id),
            paid_on=self._d(row.paid_on),
            amount=self._money(row.amount_cents),
            note=self._none_str(row.note),
            created_at=self._dt(row.created_at),
            bank_transaction_ids=sorted(int(tx.id) for tx in list(getattr(row, "bank_transactions", []) or []) if getattr(tx, "id", None) is not None),
        )

    def _serialize_bank_transaction(self, row: BankTransaction) -> BankTransactionModel:
        matched_invoice_number: str | None = None
        matched_invoice = getattr(row, "matched_invoice", None)
        if matched_invoice is not None:
            matched_invoice_number = self._none_str(getattr(matched_invoice, "number", None))
        return BankTransactionModel(
            id=int(row.id),
            subject_bank_account_id=int(row.subject_bank_account_id),
            bank_account_label=self._none_str(getattr(getattr(row, "bank_account", None), "label", None)),
            provider=str(row.provider or ""),
            external_id=str(row.external_id or ""),
            booked_on=self._d(row.booked_on),
            amount=self._money(row.amount_cents),
            currency=str(row.currency or "CZK"),
            direction=str(row.direction or ""),
            variable_symbol=self._none_str(row.variable_symbol),
            constant_symbol=self._none_str(row.constant_symbol),
            specific_symbol=self._none_str(row.specific_symbol),
            counterparty_account=self._none_str(row.counterparty_account),
            counterparty_name=self._none_str(row.counterparty_name),
            message=self._none_str(row.message),
            matched_invoice_id=(int(row.matched_invoice_id) if row.matched_invoice_id is not None else None),
            matched_invoice_number=matched_invoice_number,
            payment_id=(int(row.payment_id) if row.payment_id is not None else None),
            matched_at=self._dt(row.matched_at),
            created_at=self._dt(row.created_at),
        )

    def _serialize_bank_incoming_email(self, row: BankIncomingEmail, *, preview_limit: int = 200) -> BankIncomingEmailModel:
        return BankIncomingEmailModel(
            id=int(row.id),
            subject_bank_account_id=int(row.subject_bank_account_id),
            bank_account_label=self._none_str(getattr(getattr(row, "bank_account", None), "label", None)),
            provider=str(row.provider or ""),
            external_message_id=self._none_str(row.external_message_id),
            received_at=self._dt(row.received_at),
            from_email=self._none_str(row.from_email),
            subject=self._none_str(row.subject),
            processing_status=str(row.processing_status or "stored"),
            processing_note=self._none_str(row.processing_note),
            matched_bank_transaction_id=(int(row.matched_bank_transaction_id) if row.matched_bank_transaction_id is not None else None),
            body_preview=self._body_preview(row.body_text, limit=preview_limit),
            created_at=self._dt(row.created_at),
        )

    def _serialize_invoice_email(self, row: InvoiceEmail) -> InvoiceEmailModel:
        return InvoiceEmailModel(
            id=int(row.id),
            kind=str(row.kind or "invoice"),
            from_email=str(row.from_email or ""),
            to_email=str(row.to_email or ""),
            subject=str(row.subject or ""),
            status=str(row.status or ""),
            sent_at=self._dt(row.sent_at),
            message_id=self._none_str(row.message_id),
            error_message=(
                "E-mail se nepodařilo odeslat."
                if self._none_str(row.error_message)
                else None
            ),
            created_at=self._dt(row.created_at),
        )

    def _build_public_link(self, row: Invoice, *, request: Request | None) -> InvoicePublicLinkModel:
        token = str(row.public_token or "").strip()
        subject = getattr(row, "subject", None)
        public_username = str(getattr(subject, "public_username", None) or "").strip().lower()
        if not token or not public_username:
            return InvoicePublicLinkModel(
                enabled=False,
                url=None,
                short_url=None,
                pdf_url=None,
                pdf_download_url=None,
                isdoc_url=None,
                isdoc_download_url=None,
            )
        urls = build_public_invoice_urls(
            base_url=resolve_public_base_url(
                request=request,
                configured_base_url=self.settings.public_base_url,
                trusted_proxy_ips=self.settings.trusted_proxy_ips,
            ),
            public_username=public_username,
            token=token,
            invoice_number=str(row.number or ""),
            invoice_id=int(row.id),
            secret_key=self.settings.public_link_hmac_key,
        )
        return InvoicePublicLinkModel(
            enabled=True,
            url=str(urls.get("legacy_view") or urls.get("view") or "") or None,
            short_url=str(urls.get("view") or "") or None,
            pdf_url=str(urls.get("pdf") or "") or None,
            pdf_download_url=str(urls.get("pdf_download") or "") or None,
            isdoc_url=str(urls.get("isdoc") or "") or None,
            isdoc_download_url=str(urls.get("isdoc_download") or "") or None,
        )

    def _absolute_url(self, *, request: Request | None, relative_path: str) -> str | None:
        base = resolve_public_base_url(
            request=request,
            configured_base_url=self.settings.public_base_url,
            trusted_proxy_ips=self.settings.trusted_proxy_ips,
        )
        if not base:
            return relative_path
        return f"{base}{relative_path}"

    def _invoice_document_type_label(self, document_type: str | None) -> str:
        normalized = self._normalize_invoice_document_type(document_type, strict=False)
        if normalized == "quote":
            return "Nabídka"
        if normalized == "credit_note":
            return "Dobropis"
        if normalized == "proforma":
            return "Zálohová faktura"
        return "Faktura"

    def _invoice_document_email_subject(self, document_type: str | None, number: str | None) -> str:
        return f"{self._invoice_document_type_label(document_type)} {str(number or '').strip()}".strip()

    def _default_invoice_email_body(self, *, invoice: Invoice, public_url: str | None, from_name: str) -> str:
        total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
        body = (
            "Dobrý den,\n\n"
            f"v příloze zasílám {self._invoice_document_type_label(getattr(invoice, 'document_type', 'invoice')).lower()} {invoice.number} "
            f"na částku {total_str}.\n"
            f"Splatnost: {invoice.due_date}.\n"
        )
        if public_url:
            body += f"\nVeřejný odkaz: {public_url}\n"
        body += f"\nS pozdravem,\n{from_name}\n"
        return body

    def _format_recipient_log_value(self, *, to_emails: list[str], cc_emails: list[str] | None = None) -> str:
        parts: list[str] = []
        to_list = [str(v or "").strip() for v in list(to_emails or []) if str(v or "").strip()]
        cc_list = [str(v or "").strip() for v in list(cc_emails or []) if str(v or "").strip()]
        if to_list:
            parts.append("To: " + ", ".join(to_list))
        if cc_list:
            parts.append("Cc: " + ", ".join(cc_list))
        return " | ".join(parts)[:255]

    def _validate_recipient_list(
        self,
        value: str | None,
        *,
        field: str,
        required: bool,
        error_code: str,
        error_message: str,
    ) -> list[str]:
        raw = str(value or "").strip()
        recipients = split_recipients(raw)
        if required and not recipients:
            raise ApiError(422, error_code, error_message, {"field": field})
        invalid = [addr for addr in recipients if not looks_like_email(addr)]
        if invalid:
            raise ApiError(422, error_code, error_message, {"field": field, "invalid": invalid})
        return recipients

    def _smtp_config_for_subject(self, subject: Subject | None) -> tuple[str, str, SMTPConfig]:
        subject_email = str(getattr(subject, "email", "") or "").strip() if subject is not None else ""
        subject_name = str(getattr(subject, "name", "") or "").strip() if subject is not None else ""
        from_email = str(self.settings.smtp_from_email or subject_email or "").strip()
        from_name = str(self.settings.smtp_from_name or subject_name or "").strip()
        smtp_cfg = SMTPConfig(
            host=str(self.settings.smtp_host or "").strip(),
            port=int(self.settings.smtp_port or 0),
            username=self.settings.smtp_username,
            password=self.settings.smtp_password,
            use_tls=bool(self.settings.smtp_use_tls),
            use_starttls=bool(self.settings.smtp_use_starttls),
            timeout_seconds=float(self.settings.smtp_timeout_seconds or 10.0),
            from_email=from_email,
            from_name=from_name,
        )
        return from_email, from_name, smtp_cfg

    def _normalize_recurring_interval_unit(self, value: str | None) -> str:
        normalized = str(value or "month").strip().lower() or "month"
        if normalized not in VALID_RECURRING_INTERVAL_UNITS:
            raise ApiError(422, "recurring_interval_unit_invalid", "Neplatná jednotka opakování.", {"field": "interval_unit", "allowed": sorted(VALID_RECURRING_INTERVAL_UNITS)})
        return normalized

    def _normalize_recurring_interval_count(self, value: int | None) -> int:
        try:
            normalized = int(value or 1)
        except Exception as exc:
            raise ApiError(422, "recurring_interval_count_invalid", "Interval opakování musí být celé kladné číslo.", {"field": "interval_count"}) from exc
        if normalized < 1:
            raise ApiError(422, "recurring_interval_count_invalid", "Interval opakování musí být alespoň 1.", {"field": "interval_count"})
        return normalized

    def _normalize_recurring_due_in_days(self, value: int | None) -> int:
        try:
            normalized = int(value if value is not None else 14)
        except Exception as exc:
            raise ApiError(422, "recurring_due_in_days_invalid", "Splatnost musí být celé nezáporné číslo dnů.", {"field": "due_in_days"}) from exc
        if normalized < 0:
            raise ApiError(422, "recurring_due_in_days_invalid", "Splatnost musí být celé nezáporné číslo dnů.", {"field": "due_in_days"})
        return normalized

    def _normalize_recurring_email_override(self, value: str | None) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if not looks_like_email(raw):
            raise ApiError(422, "recurring_email_override_invalid", "Override e-mail příjemce není platná adresa.", {"field": "email_override"})
        return raw

    def _resolve_recurring_plan_name(self, value: str | None, *, template_invoice: Invoice | None) -> str:
        raw = str(value or "").strip()
        if raw:
            return raw[:255]
        invoice_number = str(getattr(template_invoice, "number", None) or "").strip()
        fallback = f"Opakování {invoice_number}".strip()
        return (fallback or "Opakovaný doklad")[:255]

    def _load_recurring_template_invoice(self, db: Session, *, subject_id: int, invoice_id: int) -> Invoice:
        invoice = self._load_invoice_for_subject(db, subject_id=subject_id, invoice_id=invoice_id)
        if invoice.contact is None:
            raise ApiError(409, "recurring_template_missing_contact", "Šablona opakování musí mít přiřazený kontakt.", {"template_invoice_id": int(invoice_id)})
        if self._normalize_invoice_document_type(invoice.document_type, strict=False) == "credit_note":
            raise ApiError(422, "recurring_template_credit_note_invalid", "Dobropis nelze použít jako šablonu opakování.", {"template_invoice_id": int(invoice_id)})
        return invoice

    def _add_recurrence_step(self, base_date: date, *, interval_unit: str, interval_count: int) -> date:
        count = self._normalize_recurring_interval_count(interval_count)
        unit = self._normalize_recurring_interval_unit(interval_unit)
        if unit == "week":
            return base_date + timedelta(days=7 * count)
        month_index = (int(base_date.month) - 1) + count
        year = int(base_date.year) + (month_index // 12)
        month = (month_index % 12) + 1
        next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last_day = (next_month_first - timedelta(days=1)).day
        day = min(int(base_date.day), last_day)
        return date(year, month, day)

    def _shift_months(self, value: date, month_delta: int) -> date:
        month_index = (int(value.month) - 1) + int(month_delta)
        year = int(value.year) + (month_index // 12)
        month = (month_index % 12) + 1
        next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last_day = (next_month_first - timedelta(days=1)).day
        day = min(int(value.day), last_day)
        return date(year, month, day)

    def _month_end(self, value: date) -> date:
        next_month_first = date(int(value.year) + 1, 1, 1) if int(value.month) == 12 else date(int(value.year), int(value.month) + 1, 1)
        return next_month_first - timedelta(days=1)

    def _shift_recurring_token_date(self, base_date: date, offset: str | None) -> date:
        raw = str(offset or "").strip().lower()
        if not raw:
            return base_date
        if raw == "++":
            raw = "+1m"
        elif raw == "--":
            raw = "-1m"
        match = re.fullmatch(r"([+-])\s*(\d+)\s*([dwmy]?)", raw)
        if not match:
            return base_date
        sign, amount_raw, unit = match.groups()
        amount = int(amount_raw or "0")
        if sign == "-":
            amount *= -1
        unit = unit or "m"
        if unit == "d":
            return base_date + timedelta(days=amount)
        if unit == "w":
            return base_date + timedelta(days=7 * amount)
        if unit == "y":
            return self._shift_months(base_date, 12 * amount)
        return self._shift_months(base_date, amount)

    def _recurring_token_map(self, issue_date: date, *, offset: str | None = None) -> dict[str, str]:
        shifted_date = self._shift_recurring_token_date(issue_date, offset)
        month_start = date(int(shifted_date.year), int(shifted_date.month), 1)
        month_end = self._month_end(shifted_date)
        month_number = f"{int(shifted_date.month):02d}"
        month_name = RECURRING_MONTH_LABELS[int(shifted_date.month) - 1]
        period_label = f"{month_name} {int(shifted_date.year)}"
        return {
            "year": str(int(shifted_date.year)),
            "month": month_number,
            "month_name": month_name,
            "period_label": period_label,
            "issue_date": shifted_date.isoformat(),
            "month_start": month_start.isoformat(),
            "month_end": month_end.isoformat(),
            "period_start": month_start.isoformat(),
            "period_end": month_end.isoformat(),
        }

    def _render_recurring_tokens(self, value: str | None, *, issue_date: date) -> str | None:
        if value is None:
            return None
        text = str(value)
        token_pattern = re.compile(
            r"\{\{\s*(year|month|month_name|period_label|issue_date|month_start|month_end|period_start|period_end)\s*"
            r"(\+\+|--|[+-]\s*\d+\s*[dwmyDWMY]?)?\s*\}\}"
        )

        def replace_token(match: re.Match[str]) -> str:
            token_name = match.group(1)
            offset = match.group(2)
            token_map = self._recurring_token_map(issue_date, offset=offset)
            return token_map.get(token_name, match.group(0))

        text = token_pattern.sub(replace_token, text)
        normalized = text.strip()
        return normalized or None

    def _clone_invoice_from_template(
        self,
        db: Session,
        *,
        template_invoice: Invoice,
        subject: Subject | None,
        issue_date: date,
        due_date: date,
    ) -> Invoice:
        template_invoice = self._load_recurring_template_invoice(
            db,
            subject_id=int(template_invoice.subject_id),
            invoice_id=int(template_invoice.id),
        )
        contact = template_invoice.contact
        if contact is None:
            raise ApiError(409, "recurring_template_missing_contact", "Šablona opakování musí mít přiřazený kontakt.", {"template_invoice_id": int(template_invoice.id)})
        document_type = self._normalize_invoice_document_type(template_invoice.document_type, strict=False)
        sid = int(template_invoice.subject_id)
        selected_series = self._resolve_existing_series(db, subject_id=sid, series_id=template_invoice.series_id)
        if selected_series is None:
            selected_series = self._resolve_invoice_series(db, subject_id=sid, document_type=document_type, requested_series_id=None)

        selected_bank_account: SubjectBankAccount | None = None
        if template_invoice.bank_account_id is not None:
            try:
                selected_bank_account = self._resolve_bank_account_selection(
                    db,
                    subject_id=sid,
                    requested_bank_account_id=int(template_invoice.bank_account_id),
                    currency=str(template_invoice.currency or getattr(subject, "default_currency", None) or "CZK"),
                )
            except ApiError:
                selected_bank_account = None
        elif str(template_invoice.payment_method or "bank_transfer") == "bank_transfer":
            selected_bank_account = self._default_subject_bank_account(
                db,
                subject_id=sid,
                currency=str(template_invoice.currency or getattr(subject, "default_currency", None) or "CZK"),
            )

        invoice = Invoice(
            subject_id=sid,
            number="DRAFT-temp",
            status="draft",
            issue_date=issue_date,
            due_date=due_date,
            currency=str(template_invoice.currency or getattr(subject, "default_currency", None) or "CZK").upper(),
            invoice_style=self._normalize_invoice_style(getattr(template_invoice, "invoice_style", None)),
            variable_symbol=self._contact_fixed_variable_symbol(contact) or self._normalize_variable_symbol_field(template_invoice.variable_symbol),
            notes=self._render_recurring_tokens(template_invoice.notes, issue_date=issue_date),
            internal_notes=self._coalesce_optional_text(template_invoice.internal_notes),
            payment_method=str(template_invoice.payment_method or "bank_transfer"),
            footer_mode=str(template_invoice.footer_mode or self._default_invoice_footer_mode(subject)),
            footer_text=self._render_recurring_tokens(template_invoice.footer_text, issue_date=issue_date),
            document_type=document_type,
            source_invoice_id=int(template_invoice.id),
            contact_id=int(contact.id),
            buyer_name_cache=str(contact.name or "") or None,
            buyer_registration_no_cache=self._coalesce_optional_text(contact.ico),
            discount_cents=int(template_invoice.discount_cents or 0),
            rounding_adjustment_cents=int(template_invoice.rounding_adjustment_cents or 0),
            total_cents=0,
            series_id=(int(selected_series.id) if selected_series is not None else None),
        )
        db.add(invoice)
        db.flush()
        invoice.number = f"DRAFT-{int(invoice.id)}"
        ensure_invoice_public_link(db, invoice=invoice, subject=subject)
        self._sync_invoice_parties(db, invoice=invoice, subject=subject, contact=contact, sync_existing=True)
        if selected_bank_account is not None:
            self._apply_invoice_bank_account_snapshot(invoice, account=selected_bank_account, subject=subject, allow_subject_fallback=True)
        else:
            invoice.bank_account_id = None
            invoice.bank_account_label = self._none_str(template_invoice.bank_account_label)
            invoice.bank_account_number = self._none_str(template_invoice.bank_account_number)
            invoice.bank_account_iban = self._none_str(template_invoice.bank_account_iban)
            invoice.bank_account_bic = self._none_str(template_invoice.bank_account_bic)
            invoice.bank_account_country = self._none_str(template_invoice.bank_account_country)

        source_items = db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == int(template_invoice.id))
            .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
        ).all()
        self._replace_invoice_items(
            db,
            invoice_id=int(invoice.id),
            items_payload=[
                {
                    "description": self._render_recurring_tokens(str(getattr(item, "description", "") or ""), issue_date=issue_date) or "",
                    "quantity": getattr(item, "quantity"),
                    "unit": self._normalize_invoice_item_unit(getattr(item, "unit", "")),
                    "unit_price_cents": int(getattr(item, "unit_price_cents", 0) or 0),
                    "vat_rate": getattr(item, "vat_rate"),
                    "line_net_cents": int(getattr(item, "line_net_cents", 0) or 0),
                    "line_vat_cents": int(getattr(item, "line_vat_cents", 0) or 0),
                    "line_total_cents": int(getattr(item, "line_total_cents", 0) or 0),
                }
                for item in source_items
            ],
        )
        self._recalc_invoice_total_cents(db, invoice=invoice)
        db.flush()
        return invoice

    def _issue_invoice_object(self, db: Session, *, invoice: Invoice, subject: Subject | None) -> Invoice:
        sid = int(invoice.subject_id)
        contact = self._load_contact_for_subject(db, subject_id=sid, contact_id=int(invoice.contact_id))
        source_invoice = self._resolve_invoice_source(
            db,
            subject_id=sid,
            document_type=str(invoice.document_type or "invoice"),
            source_invoice_id=invoice.source_invoice_id,
            current_invoice_id=int(invoice.id),
        )
        self._validate_credit_note_amount(
            db,
            document_type=str(invoice.document_type or "invoice"),
            source_invoice=source_invoice,
            current_invoice_id=int(invoice.id),
            proposed_total_cents=int(invoice.total_cents or 0),
        )
        selected_series = self._resolve_existing_series(db, subject_id=sid, series_id=invoice.series_id)
        if selected_series is None:
            selected_series = self._resolve_invoice_series(
                db,
                subject_id=sid,
                document_type=str(invoice.document_type or "invoice"),
                requested_series_id=None,
            )
            invoice.series_id = int(selected_series.id)
        selected_bank_account: SubjectBankAccount | None = None
        if invoice.bank_account_id is not None:
            selected_bank_account = self._resolve_bank_account_selection(
                db,
                subject_id=sid,
                requested_bank_account_id=int(invoice.bank_account_id),
                currency=str(invoice.currency or getattr(subject, "default_currency", None) or "CZK"),
            )
        elif not (invoice.bank_account_number or invoice.bank_account_iban):
            selected_bank_account = self._default_subject_bank_account(
                db,
                subject_id=sid,
                currency=str(invoice.currency or getattr(subject, "default_currency", None) or "CZK"),
            )
        invoice.number = self._allocate_next_invoice_number(
            db,
            subject_id=sid,
            series_id=int(selected_series.id),
            invoice_id=int(invoice.id),
            issue_date=invoice.issue_date,
        )
        if not self._normalize_variable_symbol_field(invoice.variable_symbol):
            invoice.variable_symbol = self._contact_fixed_variable_symbol(contact) or variable_symbol_from_invoice_number(invoice.number)
        invoice.status = "issued"
        invoice.issued_at = utc_now()
        ensure_invoice_public_link(db, invoice=invoice, subject=subject)
        self._sync_invoice_parties(db, invoice=invoice, subject=subject, contact=contact, sync_existing=True)
        if selected_bank_account is not None:
            self._apply_invoice_bank_account_snapshot(invoice, account=selected_bank_account, subject=subject, allow_subject_fallback=True)
        elif not (invoice.bank_account_number or invoice.bank_account_iban):
            self._apply_invoice_bank_account_snapshot(invoice, account=None, subject=subject, allow_subject_fallback=True)
        self._recalc_invoice_total_cents(db, invoice=invoice)
        db.flush()
        return invoice

    def _send_invoice_email_automatically(
        self,
        request: Request | None,
        db: Session,
        *,
        invoice: Invoice,
        subject: Subject | None,
        recipient_override: str | None = None,
    ) -> tuple[bool, str | None]:
        contact = self._load_contact_for_subject(db, subject_id=int(invoice.subject_id), contact_id=int(invoice.contact_id))
        try:
            recipients = self._validate_recipient_list(
                recipient_override if recipient_override is not None else self._none_str(contact.email),
                field="to",
                required=True,
                error_code="invoice_email_recipient_invalid",
                error_message="Neplatný e-mail příjemce.",
            )
        except ApiError as exc:
            return False, exc.message

        from_email, from_name, smtp_cfg = self._smtp_config_for_subject(subject)
        if not str(smtp_cfg.host or "").strip():
            return False, "SMTP není nastavené pro odesílání z API."
        if not looks_like_email(from_email):
            return False, "Chybí odesílatel (From). Nastav email subjektu nebo SMTP_FROM_EMAIL."

        ensure_invoice_public_link(db, invoice=invoice, subject=subject)
        public_link = self._build_public_link(invoice, request=request)
        public_url = public_link.short_url or public_link.url
        email_subject = self._invoice_document_email_subject(invoice.document_type, invoice.number)
        body = self._default_invoice_email_body(invoice=invoice, public_url=public_url, from_name=from_name)
        try:
            pdf_bytes = self._invoice_pdf_bytes(db, invoice=invoice)
        except ApiError as exc:
            return False, exc.message
        filename = f"{safe_filename_base(str(invoice.number or ''), fallback=f'invoice-{int(invoice.id)}')}.pdf"
        email_row = InvoiceEmail(
            invoice_id=int(invoice.id),
            kind="invoice",
            from_email=from_email,
            to_email=self._format_recipient_log_value(to_emails=recipients),
            subject=email_subject[:255],
            body=body,
            status="queued",
        )
        db.add(email_row)
        db.flush()
        try:
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=recipients,
                cc_emails=None,
                subject=email_subject,
                body=body,
                attachment_pdf=(filename, pdf_bytes),
            )
            message_id, _smtp_debug = send_via_smtp(smtp_cfg, msg)
            email_row.status = "sent"
            email_row.sent_at = utc_now()
            email_row.message_id = (message_id or "")[:255] if message_id else None
            email_row.error_message = None
            if str(invoice.status or "").strip().lower() == "issued":
                invoice.status = "sent"
                invoice.sent_at = utc_now()
            db.add(invoice)
            db.add(email_row)
            db.flush()
            return True, None
        except Exception as exc:
            email_row.status = "error"
            email_row.sent_at = None
            logging.getLogger("fakturek").error(
                "Automatic API invoice email failed for invoice %s (error_type=%s)",
                getattr(invoice, "id", "?"),
                type(exc).__name__,
            )
            email_row.error_message = "E-mail se nepodařilo odeslat."
            db.add(email_row)
            db.flush()
            return False, "E-mail se nepodařilo odeslat."

    def _run_recurring_plan_once(
        self,
        db: Session,
        *,
        plan: RecurringInvoicePlan,
        subject: Subject | None,
        request: Request | None,
        force: bool,
    ) -> tuple[Invoice | None, bool, list[str]]:
        if not bool(plan.is_active):
            raise ApiError(409, "recurring_plan_inactive", "Pozastavený plán nejde spustit. Nejdřív ho znovu aktivuj.", {"plan_id": int(plan.id)})
        template_invoice = self._load_recurring_template_invoice(
            db,
            subject_id=int(plan.subject_id),
            invoice_id=int(plan.template_invoice_id),
        )
        today_local = date.today()
        run_date = today_local if bool(force) else (plan.next_issue_date or today_local)
        if run_date > today_local and not bool(force):
            return None, False, []
        created_invoice = self._clone_invoice_from_template(
            db,
            template_invoice=template_invoice,
            subject=subject,
            issue_date=run_date,
            due_date=run_date + timedelta(days=int(plan.due_in_days or 14)),
        )
        errors: list[str] = []
        emailed = False
        if bool(plan.auto_issue):
            self._issue_invoice_object(db, invoice=created_invoice, subject=subject)
        if bool(plan.auto_send) and str(created_invoice.status or "").strip().lower() != "draft":
            email_ok, email_error = self._send_invoice_email_automatically(
                request,
                db,
                invoice=created_invoice,
                subject=subject,
                recipient_override=self._none_str(plan.email_override),
            )
            emailed = bool(email_ok)
            if email_error:
                errors.append(str(email_error))
        plan.last_run_at = utc_now()
        plan.last_generated_invoice_id = int(created_invoice.id)
        next_date = self._add_recurrence_step(
            run_date,
            interval_unit=str(plan.interval_unit or "month"),
            interval_count=int(plan.interval_count or 1),
        )
        while next_date <= today_local:
            next_date = self._add_recurrence_step(
                next_date,
                interval_unit=str(plan.interval_unit or "month"),
                interval_count=int(plan.interval_count or 1),
            )
        plan.next_issue_date = next_date
        db.add(plan)
        db.flush()
        return created_invoice, emailed, errors

    def _invoice_pdf_bytes(self, db: Session, *, invoice: Invoice) -> bytes:
        cached_path = str(invoice.pdf_path or "").strip()
        if cached_path and str(invoice.status or "").strip().lower() != "draft":
            cached = read_pdf_bytes(self.pdf_storage_root, cached_path)
            if cached is not None and bytes(cached).startswith(b"%PDF"):
                return bytes(cached)

        pdf_data = self._build_invoice_pdf_data(db, invoice=invoice)
        try:
            pdf_bytes = render_invoice_pdf_bytes(pdf_data)
        except Exception as exc:
            raise ApiError(500, "invoice_pdf_generation_failed", "Nepodařilo se vygenerovat PDF dokladu.", {"invoice_id": int(invoice.id)}) from exc

        if str(invoice.status or "").strip().lower() != "draft":
            relpath, digest = persist_pdf_bytes(
                storage_root=self.pdf_storage_root,
                subject_id=int(invoice.subject_id),
                invoice_id=int(invoice.id),
                invoice_number=str(invoice.number or ""),
                pdf_bytes=bytes(pdf_bytes),
            )
            invoice.pdf_path = relpath
            invoice.pdf_hash = digest
            invoice.pdf_generated_at = utc_now()
            db.add(invoice)
            db.flush()
        return bytes(pdf_bytes)

    def _build_invoice_pdf_data(self, db: Session, *, invoice: Invoice) -> InvoicePDFData:
        buyer = self._invoice_party_snapshot(invoice, role="buyer")
        seller = self._invoice_party_snapshot(invoice, role="seller")
        if not buyer and getattr(invoice, "contact", None) is not None:
            contact = invoice.contact
            buyer = {
                "name": str(getattr(contact, "name", "") or ""),
                "email": str(getattr(contact, "email", "") or ""),
                "phone": str(getattr(contact, "phone", "") or ""),
                "street": str(getattr(contact, "street", "") or ""),
                "city": str(getattr(contact, "city", "") or ""),
                "zip": str(getattr(contact, "zip", "") or ""),
                "country": str(getattr(contact, "country", "") or ""),
                "ico": str(getattr(contact, "ico", "") or ""),
                "dic": str(getattr(contact, "dic", "") or ""),
            }
        if not seller:
            subject = getattr(invoice, "subject", None) or db.scalar(select(Subject).where(Subject.id == int(invoice.subject_id)).limit(1))
            seller = {
                "name": str(getattr(subject, "name", "") or ""),
                "email": str(getattr(subject, "email", "") or ""),
                "phone": str(getattr(subject, "phone", "") or ""),
                "street": str(getattr(subject, "street", "") or ""),
                "city": str(getattr(subject, "city", "") or ""),
                "zip": str(getattr(subject, "zip", "") or ""),
                "country": str(getattr(subject, "country", "") or ""),
                "ico": str(getattr(subject, "ico", "") or ""),
                "dic": str(getattr(subject, "dic", "") or ""),
            }

        items = [
            {
                "description": str(item.description or ""),
                "quantity": self._decimal(item.quantity),
                "unit": str(item.unit or ""),
                "unit_price_cents": int(item.unit_price_cents or 0),
                "vat_rate": self._decimal(item.vat_rate),
                "line_total_cents": int(item.line_total_cents or 0),
            }
            for item in sorted(list(invoice.items or []), key=lambda x: (x.sort_order, x.id))
        ]
        source_invoice_number: str | None = None
        if invoice.source_invoice_id is not None:
            source_invoice_number = db.scalar(select(Invoice.number).where(Invoice.id == int(invoice.source_invoice_id)).limit(1))

        return InvoicePDFData(
            number=str(invoice.number or ""),
            status=str(invoice.status or ""),
            language=str(getattr(invoice, "invoice_language", "") or "cs"),
            invoice_style=self._coerce_invoice_style(getattr(invoice, "invoice_style", None)),
            issue_date=invoice.issue_date,
            taxable_supply_date=getattr(invoice, "taxable_supply_date", None) or invoice.issue_date,
            due_date=invoice.due_date,
            currency=str(invoice.currency or "CZK"),
            items_total_cents=sum(int(item.get("line_total_cents") or 0) for item in items),
            discount_cents=int(invoice.discount_cents or 0),
            rounding_adjustment_cents=int(invoice.rounding_adjustment_cents or 0),
            total_cents=int(invoice.total_cents or 0),
            notes=self._none_str(invoice.notes),
            issuer=seller,
            customer=buyer,
            items=items,
            document_type=str(invoice.document_type or "invoice"),
            document_label=self._invoice_document_type_label(invoice.document_type),
            payment_method=str(invoice.payment_method or "bank_transfer"),
            variable_symbol=str(invoice.variable_symbol or ""),
            footer_text=self._none_str(invoice.footer_text),
            source_invoice_number=self._none_str(source_invoice_number),
            payment_account=self._invoice_payment_account_payload(invoice),
            payment_qr_codes=[],
        )

    def _invoice_party_snapshot(self, invoice: Invoice, *, role: str) -> dict[str, str]:
        for party in list(invoice.parties or []):
            if str(getattr(party, "role", "") or "").strip().lower() == str(role).strip().lower():
                return {
                    "name": str(getattr(party, "name", "") or ""),
                    "email": str(getattr(party, "email", "") or ""),
                    "phone": str(getattr(party, "phone", "") or ""),
                    "street": str(getattr(party, "street", "") or ""),
                    "city": str(getattr(party, "city", "") or ""),
                    "zip": str(getattr(party, "zip", "") or ""),
                    "country": str(getattr(party, "country", "") or ""),
                    "ico": str(getattr(party, "ico", "") or ""),
                    "dic": str(getattr(party, "dic", "") or ""),
                }
        return {}

    def _invoice_payment_account_payload(self, invoice: Invoice) -> dict[str, str]:
        account_number = self._none_str(invoice.bank_account_number)
        iban = self._none_str(invoice.bank_account_iban)
        bic = self._none_str(invoice.bank_account_bic)
        country = self._none_str(invoice.bank_account_country)
        label = self._none_str(invoice.bank_account_label)
        if not account_number and not iban:
            return {}
        return {
            "label": str(label or account_number or format_iban_for_display(iban) or ""),
            "number": str(account_number or ""),
            "display": str(account_number or format_iban_for_display(iban) or ""),
            "iban": str(format_iban_for_display(iban) if iban else ""),
            "bic": str(bic or ""),
            "country": str(country or ""),
        }

    def _apply_invoice_filters(self, stmt, count_stmt, filters: InvoiceListFilters):
        term = str(filters.q or "").strip()
        if term:
            pattern = f"%{term}%"
            cond = or_(
                Invoice.number.ilike(pattern),
                Invoice.buyer_name_cache.ilike(pattern),
                Invoice.variable_symbol.ilike(pattern),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if filters.status:
            stmt = stmt.where(Invoice.status == str(filters.status))
            count_stmt = count_stmt.where(Invoice.status == str(filters.status))
        if filters.document_type:
            stmt = stmt.where(Invoice.document_type == str(filters.document_type))
            count_stmt = count_stmt.where(Invoice.document_type == str(filters.document_type))
        if filters.contact_id is not None:
            stmt = stmt.where(Invoice.contact_id == int(filters.contact_id))
            count_stmt = count_stmt.where(Invoice.contact_id == int(filters.contact_id))
        if filters.issue_date_from is not None:
            stmt = stmt.where(Invoice.issue_date >= filters.issue_date_from)
            count_stmt = count_stmt.where(Invoice.issue_date >= filters.issue_date_from)
        if filters.issue_date_to is not None:
            stmt = stmt.where(Invoice.issue_date <= filters.issue_date_to)
            count_stmt = count_stmt.where(Invoice.issue_date <= filters.issue_date_to)
        if filters.overdue is True:
            today = date.today()
            stmt = stmt.where(Invoice.due_date < today).where(Invoice.status != "paid")
            count_stmt = count_stmt.where(Invoice.due_date < today).where(Invoice.status != "paid")
        if filters.overdue is False:
            today = date.today()
            stmt = stmt.where(or_(Invoice.due_date >= today, Invoice.status == "paid"))
            count_stmt = count_stmt.where(or_(Invoice.due_date >= today, Invoice.status == "paid"))
        return stmt, count_stmt

    def _load_contact_for_subject(self, db: Session, *, subject_id: int, contact_id: int) -> Contact:
        row = db.scalar(
            select(Contact)
            .where(Contact.subject_id == int(subject_id))
            .where(Contact.id == int(contact_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "contact_not_found", "Kontakt nebyl nalezen.", {"contact_id": int(contact_id)})
        return row

    def _load_invoice_series_for_subject(self, db: Session, *, subject_id: int, series_id: int) -> InvoiceSeries:
        row = db.scalar(
            select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == int(subject_id))
            .where(InvoiceSeries.id == int(series_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "invoice_series_not_found", "Číselná řada nebyla nalezena.", {"series_id": int(series_id)})
        return row

    def _load_bank_account_for_subject(self, db: Session, *, subject_id: int, bank_account_id: int) -> SubjectBankAccount:
        row = db.scalar(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .where(SubjectBankAccount.id == int(bank_account_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "bank_account_not_found", "Bankovní účet nebyl nalezen.", {"bank_account_id": int(bank_account_id)})
        return row

    def _load_catalog_item_for_subject(self, db: Session, *, subject_id: int, item_id: int) -> InvoiceCatalogItem:
        row = db.scalar(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .where(InvoiceCatalogItem.id == int(item_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "catalog_item_not_found", "Katalogová položka nebyla nalezena.", {"item_id": int(item_id)})
        return row

    def _load_recurring_plan_for_subject(self, db: Session, *, subject_id: int, plan_id: int) -> RecurringInvoicePlan:
        row = db.scalar(
            select(RecurringInvoicePlan)
            .execution_options(populate_existing=True)
            .options(joinedload(RecurringInvoicePlan.template_invoice))
            .where(RecurringInvoicePlan.subject_id == int(subject_id))
            .where(RecurringInvoicePlan.id == int(plan_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "recurring_plan_not_found", "Plán opakování nebyl nalezen.", {"plan_id": int(plan_id)})
        return row

    def _load_invoice_for_subject(self, db: Session, *, subject_id: int, invoice_id: int) -> Invoice:
        row = db.scalar(
            select(Invoice)
            .execution_options(populate_existing=True)
            .options(
                joinedload(Invoice.contact),
                joinedload(Invoice.items),
                joinedload(Invoice.parties),
                joinedload(Invoice.payments).joinedload(Payment.bank_transactions),
                joinedload(Invoice.subject),
            )
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.id == int(invoice_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "invoice_not_found", "Doklad nebyl nalezen.", {"invoice_id": int(invoice_id)})
        return row

    def _load_payment_for_invoice(self, db: Session, *, invoice_id: int, payment_id: int) -> Payment:
        row = db.scalar(
            select(Payment)
            .execution_options(populate_existing=True)
            .options(joinedload(Payment.bank_transactions))
            .where(Payment.invoice_id == int(invoice_id))
            .where(Payment.id == int(payment_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "payment_not_found", "Platba nebyla nalezena.", {"payment_id": int(payment_id)})
        return row

    def _load_bank_transaction_for_subject(self, db: Session, *, subject_id: int, transaction_id: int) -> BankTransaction:
        row = db.scalar(
            select(BankTransaction)
            .execution_options(populate_existing=True)
            .options(
                joinedload(BankTransaction.bank_account),
                joinedload(BankTransaction.matched_invoice),
                joinedload(BankTransaction.payment),
            )
            .join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .where(BankTransaction.id == int(transaction_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "bank_transaction_not_found", "Bankovní transakce nebyla nalezena.", {"transaction_id": int(transaction_id)})
        return row

    def _load_bank_incoming_email_for_subject(self, db: Session, *, subject_id: int, email_id: int) -> BankIncomingEmail:
        row = db.scalar(
            select(BankIncomingEmail)
            .execution_options(populate_existing=True)
            .options(
                joinedload(BankIncomingEmail.bank_account),
                joinedload(BankIncomingEmail.matched_bank_transaction),
            )
            .join(SubjectBankAccount, SubjectBankAccount.id == BankIncomingEmail.subject_bank_account_id)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .where(BankIncomingEmail.id == int(email_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(404, "bank_incoming_email_not_found", "Bankovní notifikační e-mail nebyl nalezen.", {"email_id": int(email_id)})
        return row

    def _contact_payload_from_request(self, payload: ContactCreateRequest) -> dict[str, Any]:
        normalized_email = self._validate_contact_email_input(payload.email)
        name = str(payload.name or "").strip()
        if not name:
            raise ApiError(422, "contact_name_required", "Jméno je povinné.", {"field": "name"})
        return {
            "name": name,
            "email": normalized_email or None,
            "phone": self._coalesce_optional_text(payload.phone),
            "street": self._coalesce_optional_text(payload.street),
            "city": self._coalesce_optional_text(payload.city),
            "zip": self._coalesce_optional_text(payload.zip),
            "country": self._coalesce_optional_country(payload.country),
            "ico": self._coalesce_optional_text(payload.ico),
            "dic": self._coalesce_optional_text(payload.dic),
            "fixed_variable_symbol": self._normalize_fixed_variable_symbol(payload.fixed_variable_symbol),
        }

    def _contact_patch_from_request(self, payload: ContactPatchRequest) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        fields_set = set(payload.model_fields_set)
        if "name" in fields_set:
            name = str(payload.name or "").strip()
            if not name:
                raise ApiError(422, "contact_name_required", "Jméno je povinné.", {"field": "name"})
            updates["name"] = name
        if "email" in fields_set:
            updates["email"] = self._validate_contact_email_input(payload.email) or None
        if "phone" in fields_set:
            updates["phone"] = self._coalesce_optional_text(payload.phone)
        if "street" in fields_set:
            updates["street"] = self._coalesce_optional_text(payload.street)
        if "city" in fields_set:
            updates["city"] = self._coalesce_optional_text(payload.city)
        if "zip" in fields_set:
            updates["zip"] = self._coalesce_optional_text(payload.zip)
        if "country" in fields_set:
            updates["country"] = self._coalesce_optional_country(payload.country)
        if "ico" in fields_set:
            updates["ico"] = self._coalesce_optional_text(payload.ico)
        if "dic" in fields_set:
            updates["dic"] = self._coalesce_optional_text(payload.dic)
        if "fixed_variable_symbol" in fields_set:
            updates["fixed_variable_symbol"] = self._normalize_fixed_variable_symbol(payload.fixed_variable_symbol)
        return updates

    def _catalog_item_values_from_request(self, payload: CatalogItemCreateRequest, *, subject: Subject | None) -> dict[str, Any]:
        description = str(payload.description or "").strip()
        if not description:
            raise ApiError(422, "catalog_item_description_required", "Popis katalogové položky je povinný.", {"field": "description"})
        is_vat_payer = bool(getattr(subject, "is_vat_payer", False))
        try:
            quantity = parse_quantity(payload.quantity)
        except ValueError as exc:
            raise ApiError(422, "catalog_item_quantity_invalid", str(exc), {"field": "quantity"}) from exc
        try:
            unit_price_cents = parse_money_to_cents(payload.unit_price)
        except ValueError as exc:
            raise ApiError(422, "catalog_item_unit_price_invalid", str(exc), {"field": "unit_price"}) from exc
        if not is_vat_payer:
            vat_rate = Decimal("0.00")
        else:
            try:
                vat_rate = parse_vat_rate(payload.vat_rate)
            except ValueError as exc:
                raise ApiError(422, "catalog_item_vat_rate_invalid", str(exc), {"field": "vat_rate"}) from exc
        return {
            "description": description,
            "quantity": quantity,
            "unit": str(payload.unit or "").strip(),
            "unit_price_cents": unit_price_cents,
            "vat_rate": vat_rate,
            "currency": self._normalize_currency(payload.currency or getattr(subject, "default_currency", None) or "CZK"),
        }

    def _catalog_item_patch_values_from_request(
        self,
        payload: CatalogItemPatchRequest,
        *,
        current: InvoiceCatalogItem,
        subject: Subject | None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        fields_set = set(payload.model_fields_set)
        is_vat_payer = bool(getattr(subject, "is_vat_payer", False))

        if "description" in fields_set:
            description = str(payload.description or "").strip()
            if not description:
                raise ApiError(422, "catalog_item_description_required", "Popis katalogové položky je povinný.", {"field": "description"})
            updates["description"] = description
        if "quantity" in fields_set:
            if payload.quantity is None:
                raise ApiError(422, "catalog_item_quantity_required", "Množství je povinné.", {"field": "quantity"})
            try:
                updates["quantity"] = parse_quantity(payload.quantity)
            except ValueError as exc:
                raise ApiError(422, "catalog_item_quantity_invalid", str(exc), {"field": "quantity"}) from exc
        if "unit" in fields_set:
            updates["unit"] = str(payload.unit or "").strip()
        if "unit_price" in fields_set:
            if payload.unit_price is None:
                raise ApiError(422, "catalog_item_unit_price_required", "Cena je povinná.", {"field": "unit_price"})
            try:
                updates["unit_price_cents"] = parse_money_to_cents(payload.unit_price)
            except ValueError as exc:
                raise ApiError(422, "catalog_item_unit_price_invalid", str(exc), {"field": "unit_price"}) from exc
        if "vat_rate" in fields_set:
            if not is_vat_payer:
                updates["vat_rate"] = Decimal("0.00")
            else:
                if payload.vat_rate is None:
                    raise ApiError(422, "catalog_item_vat_rate_required", "Sazba DPH je povinná.", {"field": "vat_rate"})
                try:
                    updates["vat_rate"] = parse_vat_rate(payload.vat_rate)
                except ValueError as exc:
                    raise ApiError(422, "catalog_item_vat_rate_invalid", str(exc), {"field": "vat_rate"}) from exc
        if "currency" in fields_set:
            if payload.currency is None:
                raise ApiError(422, "catalog_item_currency_required", "Měna je povinná.", {"field": "currency"})
            updates["currency"] = self._normalize_currency(payload.currency)

        if not updates:
            return {
                "description": str(current.description or ""),
                "quantity": current.quantity,
                "unit": str(current.unit or ""),
                "unit_price_cents": int(current.unit_price_cents or 0),
                "vat_rate": current.vat_rate,
                "currency": str(current.currency or "CZK"),
            }
        return updates

    def _validate_contact_email_input(self, value: str | None) -> str:
        normalized = ", ".join(split_recipients(str(value or "").strip()))
        if not normalized:
            return ""
        recipients = split_recipients(normalized)
        invalid = [addr for addr in recipients if not looks_like_email(addr)]
        if invalid:
            raise ApiError(
                422,
                "contact_email_invalid",
                "Neplatný e-mail u kontaktu. Více adres odděl čárkou nebo středníkem.",
                {"field": "email", "invalid": invalid},
            )
        return normalized

    def _normalize_fixed_variable_symbol(self, value: str | None) -> str | None:
        raw = str(value or "").strip()
        digits = digits_only(raw)
        if raw and not digits:
            raise ApiError(422, "contact_fixed_variable_symbol_invalid", "Pevný VS musí obsahovat číslice.", {"field": "fixed_variable_symbol"})
        if len(digits) > 10:
            raise ApiError(422, "contact_fixed_variable_symbol_too_long", "Pevný VS může mít maximálně 10 číslic.", {"field": "fixed_variable_symbol"})
        return digits or None

    def _normalize_invoice_document_type(self, value: str | None, *, strict: bool) -> str:
        normalized = str(value or "invoice").strip().lower() or "invoice"
        if normalized not in VALID_DOCUMENT_TYPES:
            if strict:
                raise ApiError(422, "invoice_document_type_invalid", "Neplatný typ dokladu.", {"field": "document_type", "allowed": sorted(VALID_DOCUMENT_TYPES)})
            return "invoice"
        return normalized

    def _normalize_currency(self, value: str | None) -> str:
        currency = str(value or "CZK").strip().upper() or "CZK"
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ApiError(422, "invoice_currency_invalid", "Měna musí být 3písmenný kód, např. CZK nebo EUR.", {"field": "currency"})
        return currency

    def _normalize_invoice_language(self, value: str | None) -> str:
        language = str(value or "cs").strip().lower() or "cs"
        if language not in VALID_INVOICE_LANGUAGES:
            raise ApiError(
                422,
                "invoice_language_invalid",
                "Jazyk faktury musí být cs nebo en.",
                {"field": "invoice_language", "allowed": sorted(VALID_INVOICE_LANGUAGES)},
            )
        return language

    def _normalize_invoice_style(self, value: str | None) -> str:
        style = str(value or "modern").strip().lower() or "modern"
        if style not in VALID_INVOICE_STYLES:
            raise ApiError(
                422,
                "invoice_style_invalid",
                "Vzhled faktury musí být modern, classic nebo minimal.",
                {"field": "invoice_style", "allowed": sorted(VALID_INVOICE_STYLES)},
            )
        return style

    def _coerce_invoice_style(self, value: str | None) -> str:
        style = str(value or "modern").strip().lower() or "modern"
        return style if style in VALID_INVOICE_STYLES else "modern"

    def _normalize_payment_method(self, value: str | None) -> str:
        method = str(value or "bank_transfer").strip().lower() or "bank_transfer"
        if method not in VALID_PAYMENT_METHODS:
            raise ApiError(422, "invoice_payment_method_invalid", "Neplatný způsob platby.", {"field": "payment_method", "allowed": sorted(VALID_PAYMENT_METHODS)})
        return method

    def _coalesce_optional_text(self, value: str | None) -> str | None:
        raw = str(value or "").strip()
        return raw or None

    def _coalesce_optional_country(self, value: str | None) -> str | None:
        raw = str(value or "").strip().upper()
        return raw[:2] or None

    def _default_invoice_footer_mode(self, subject: Subject | None) -> str:
        stored_mode = str(getattr(subject, "default_invoice_footer_mode", None) or "").strip().lower()
        if stored_mode in VALID_FOOTER_MODES:
            return stored_mode
        name = str(getattr(subject, "name", None) or "").strip().lower()
        if any(token in name for token in (" z.s.", "z.s.", "spolek")):
            return "association_register"
        if any(token in name for token in ("s.r.o", " a.s.", " a. s.", "akciová společnost", "spol. s r.o", "k.s.", "v.o.s.", " družstvo")):
            return "commercial_register"
        return "trade_register"

    def _invoice_footer_text_for_mode(self, mode: str | None, *, subject: Subject | None = None) -> str:
        normalized = str(mode or "").strip().lower()
        if not normalized:
            normalized = self._default_invoice_footer_mode(subject)
        if normalized == "custom":
            return str(getattr(subject, "default_invoice_footer_text", None) or "")
        return str(FOOTER_PRESET_TEXTS.get(normalized, "") or "")

    def _resolve_invoice_footer(self, *, subject: Subject | None, footer_mode: str | None, footer_text: str | None) -> tuple[str, str | None]:
        normalized_mode = str(footer_mode or "").strip().lower()
        if not normalized_mode:
            normalized_mode = self._default_invoice_footer_mode(subject)
        if normalized_mode not in VALID_FOOTER_MODES:
            raise ApiError(422, "invoice_footer_mode_invalid", "Neplatný režim patičky.", {"field": "footer_mode", "allowed": sorted(VALID_FOOTER_MODES)})
        custom_text = str(footer_text or "").strip()
        if normalized_mode == "custom":
            if not custom_text:
                custom_text = self._invoice_footer_text_for_mode(normalized_mode, subject=subject)
            return normalized_mode, (custom_text or None)
        resolved_text = self._invoice_footer_text_for_mode(normalized_mode, subject=subject)
        return normalized_mode, (resolved_text or None)

    def _normalize_variable_symbol_field(self, value: str | None) -> str | None:
        digits = digits_only(value)
        if len(digits) > 10:
            raise ApiError(422, "invoice_variable_symbol_too_long", "Variabilní symbol může mít maximálně 10 číslic.", {"field": "variable_symbol"})
        return digits or None

    def _contact_fixed_variable_symbol(self, contact: Contact | None) -> str:
        return digits_only(getattr(contact, "fixed_variable_symbol", None))[:10]

    def _resolve_invoice_variable_symbol(self, *, explicit_value: str | None, contact: Contact | None) -> str | None:
        fixed = self._contact_fixed_variable_symbol(contact)
        if fixed:
            return fixed
        return self._normalize_variable_symbol_field(explicit_value)

    def _parse_invoice_items(
        self,
        items: list[InvoiceItemWriteModel],
        *,
        is_vat_payer: bool,
        allow_negative_unit_price: bool,
    ) -> list[dict[str, object]]:
        parsed_items: list[dict[str, object]] = []
        for index, raw in enumerate(list(items or []), start=1):
            description = str(raw.description or "").strip()
            if not description:
                raise ApiError(422, "invoice_item_description_required", f"Položka #{index}: vyplň popis.", {"field": f"items[{index - 1}].description"})
            try:
                quantity = parse_quantity(raw.quantity)
            except ValueError as exc:
                raise ApiError(422, "invoice_item_quantity_invalid", f"Položka #{index}: {exc}", {"field": f"items[{index - 1}].quantity"}) from exc
            try:
                if allow_negative_unit_price:
                    signed_unit_price_cents = parse_money_to_signed_cents(raw.unit_price)
                    unit_price_sign = -1 if signed_unit_price_cents < 0 else 1
                    unit_price_cents = abs(int(signed_unit_price_cents))
                else:
                    unit_price_sign = 1
                    unit_price_cents = parse_money_to_cents(raw.unit_price)
            except ValueError as exc:
                raise ApiError(422, "invoice_item_unit_price_invalid", f"Položka #{index}: {exc}", {"field": f"items[{index - 1}].unit_price"}) from exc
            try:
                vat_rate = parse_vat_rate(raw.vat_rate) if is_vat_payer else Decimal("0.00")
            except ValueError as exc:
                raise ApiError(422, "invoice_item_vat_invalid", f"Položka #{index}: {exc}", {"field": f"items[{index - 1}].vat_rate"}) from exc
            line_net_cents, line_vat_cents, line_total_cents = compute_line_amounts_cents(
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                vat_rate=vat_rate,
            )
            line_net_cents *= int(unit_price_sign)
            line_vat_cents *= int(unit_price_sign)
            line_total_cents *= int(unit_price_sign)
            signed_unit_price_cents = int(unit_price_cents) * int(unit_price_sign)
            parsed_items.append(
                {
                    "description": description,
                    "quantity": quantity,
                    "unit": self._normalize_invoice_item_unit(raw.unit),
                    "unit_price_cents": int(signed_unit_price_cents),
                    "vat_rate": vat_rate,
                    "line_net_cents": int(line_net_cents),
                    "line_vat_cents": int(line_vat_cents),
                    "line_total_cents": int(line_total_cents),
                }
            )
        return parsed_items

    def _normalize_invoice_item_unit(self, value: str | None) -> str:
        return " ".join(str(value or "").split()).strip()[:32]

    def _resolve_invoice_financials(
        self,
        *,
        items_payload: list[dict[str, object]] | None = None,
        items_total_cents: int | None = None,
        discount: str | None,
        rounding_adjustment: str | None,
        apply_auto_rounding: bool,
    ) -> dict[str, int]:
        if items_total_cents is None:
            items_total = sum(int(item.get("line_total_cents") or 0) for item in list(items_payload or []))
        else:
            items_total = int(items_total_cents or 0)
        try:
            discount_cents = parse_money_to_cents(discount)
        except ValueError as exc:
            raise ApiError(422, "invoice_discount_invalid", str(exc), {"field": "discount"}) from exc
        if discount_cents > max(items_total, 0):
            raise ApiError(422, "invoice_discount_too_high", "Sleva nesmí být vyšší než mezisoučet.", {"field": "discount"})
        try:
            rounding_adjustment_cents = parse_money_to_signed_cents(rounding_adjustment)
        except ValueError as exc:
            raise ApiError(422, "invoice_rounding_invalid", str(exc), {"field": "rounding_adjustment"}) from exc
        if apply_auto_rounding:
            rounding_adjustment_cents = compute_rounding_adjustment_cents(items_total - discount_cents)
        return {
            "items_total_cents": int(items_total),
            "discount_cents": int(discount_cents),
            "rounding_adjustment_cents": int(rounding_adjustment_cents),
            "draft_total_cents": int(items_total - discount_cents + rounding_adjustment_cents),
        }

    def _resolve_invoice_source(
        self,
        db: Session,
        *,
        subject_id: int,
        document_type: str,
        source_invoice_id: int | None,
        current_invoice_id: int | None,
    ) -> Invoice | None:
        normalized_document_type = self._normalize_invoice_document_type(document_type, strict=True)
        source_id = int(source_invoice_id) if source_invoice_id is not None else None
        if normalized_document_type == "credit_note" and source_id is None:
            raise ApiError(422, "invoice_source_required", "Dobropis musí odkazovat na původní fakturu.", {"field": "source_invoice_id"})
        if source_id is None:
            return None
        if current_invoice_id is not None and int(source_id) == int(current_invoice_id):
            raise ApiError(422, "invoice_source_invalid", "Doklad nemůže odkazovat sám na sebe.", {"field": "source_invoice_id"})
        row = db.scalar(
            select(Invoice)
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.id == int(source_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(422, "invoice_source_not_found", "Původní doklad nebyl nalezen.", {"field": "source_invoice_id", "source_invoice_id": int(source_id)})
        return row

    def _validate_credit_note_amount(
        self,
        db: Session,
        *,
        document_type: str,
        source_invoice: Invoice | None,
        current_invoice_id: int | None,
        proposed_total_cents: int,
    ) -> None:
        if self._normalize_invoice_document_type(document_type, strict=True) != "credit_note":
            return
        if source_invoice is None:
            raise ApiError(422, "invoice_source_required", "Dobropis musí odkazovat na původní fakturu.", {"field": "source_invoice_id"})
        available_credit_cents = self._credit_note_available_cents(
            db,
            source_invoice=source_invoice,
            current_credit_note_id=current_invoice_id,
        )
        proposed_credit_cents = abs(int(proposed_total_cents or 0))
        if proposed_credit_cents > available_credit_cents:
            raise ApiError(
                422,
                "invoice_credit_limit_exceeded",
                f"Dobropisem už bys překročil částku původní faktury. Zbývá dobropisovat maximálně {format_cents(available_credit_cents, str(source_invoice.currency or 'CZK'))}.",
                {"available_credit_cents": int(available_credit_cents), "proposed_credit_cents": int(proposed_credit_cents)},
            )

    def _credit_note_available_cents(
        self,
        db: Session,
        *,
        source_invoice: Invoice,
        current_credit_note_id: int | None = None,
    ) -> int:
        rows = db.scalars(
            select(Invoice)
            .where(Invoice.source_invoice_id == int(source_invoice.id))
            .where(Invoice.document_type == "credit_note")
            .where(Invoice.status != "draft")
        ).all()
        already_credited = 0
        for row in rows:
            if current_credit_note_id is not None and int(row.id) == int(current_credit_note_id):
                continue
            already_credited += abs(int(getattr(row, "total_cents", 0) or 0))
        return max(int(getattr(source_invoice, "total_cents", 0) or 0) - int(already_credited), 0)

    def _invoice_number_year(self, issue_date: date | None = None) -> int:
        return int((issue_date or date.today()).year)

    def _normalized_series_prefix(self, prefix: str | None, *, year: int) -> str:
        raw = str(prefix or "").strip()
        raw = re.sub(r"^20\d{2}[-_/\s]*", "", raw)
        raw = re.sub(r"[^A-Za-z0-9/_-]+", "-", raw).strip("-_/" )
        if raw:
            return f"{int(year)}-{raw}-"
        return f"{int(year)}-"

    def _invoice_series_definition_for_type(self, document_type: str | None) -> tuple[str, str]:
        normalized = self._normalize_invoice_document_type(document_type, strict=True)
        if normalized == "quote":
            return "quote", "NAB"
        if normalized == "credit_note":
            return "credit_note", "DOB"
        if normalized == "proforma":
            return "proforma", "ZAL"
        return "default", ""

    def _get_or_create_default_invoice_series(
        self,
        db: Session,
        *,
        subject_id: int,
        document_type: str | None = None,
    ) -> InvoiceSeries:
        series_name, series_prefix = self._invoice_series_definition_for_type(document_type)
        series = db.scalar(
            select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == int(subject_id))
            .where(InvoiceSeries.name == str(series_name))
        )
        if series is not None:
            return series
        series = InvoiceSeries(
            subject_id=int(subject_id),
            name=str(series_name),
            prefix=str(series_prefix),
            pad_length=4,
            last_counter=0,
            last_counter_year=self._invoice_number_year(),
        )
        db.add(series)
        db.flush()
        return series

    def _resolve_existing_series(self, db: Session, *, subject_id: int, series_id: int | None) -> InvoiceSeries | None:
        if series_id is None:
            return None
        return db.scalar(
            select(InvoiceSeries)
            .where(InvoiceSeries.id == int(series_id))
            .where(InvoiceSeries.subject_id == int(subject_id))
            .limit(1)
        )

    def _resolve_invoice_series(
        self,
        db: Session,
        *,
        subject_id: int,
        document_type: str,
        requested_series_id: int | None,
    ) -> InvoiceSeries | None:
        if requested_series_id is None:
            return self._get_or_create_default_invoice_series(db, subject_id=subject_id, document_type=document_type)
        series = self._resolve_existing_series(db, subject_id=subject_id, series_id=requested_series_id)
        if series is None:
            raise ApiError(422, "invoice_series_not_found", "Vybraná číselná řada neexistuje.", {"field": "series_id", "series_id": int(requested_series_id)})
        return series

    def _split_invoice_number_prefix_counter(self, number: str | None) -> tuple[str, int, int] | None:
        raw = str(number or "").strip()
        if not raw:
            return None
        match = re.search(r"(\d+)$", raw)
        if not match:
            return None
        digits = match.group(1)
        prefix = raw[: -len(digits)]
        try:
            counter = int(digits)
        except Exception:
            return None
        return prefix, counter, len(digits)

    def _observed_series_counter_for_year(self, db: Session, *, subject_id: int, series: InvoiceSeries | None, year: int) -> int:
        if series is None:
            return 0
        prefix = self._normalized_series_prefix(getattr(series, "prefix", None), year=int(year))
        rows = db.scalars(
            select(Invoice.number)
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.number.is_not(None))
            .where(Invoice.number.startswith(prefix))
        ).all()
        max_counter = 0
        for raw_number in rows:
            parts = self._split_invoice_number_prefix_counter(raw_number)
            if parts is None:
                continue
            number_prefix, counter, _digits_len = parts
            if str(number_prefix) != str(prefix):
                continue
            if int(counter) > int(max_counter):
                max_counter = int(counter)
        return int(max_counter)

    def _sync_series_counter_for_year(self, db: Session, *, subject_id: int, series: InvoiceSeries | None, year: int) -> int:
        if series is None:
            return 0
        observed = self._observed_series_counter_for_year(db, subject_id=int(subject_id), series=series, year=int(year))
        try:
            last_year = int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None
        except Exception:
            last_year = None
        current = int(series.last_counter or 0) if last_year == int(year) else 0
        effective = max(int(current), int(observed))
        if effective > int(current) or (effective > 0 and last_year != int(year)):
            series.last_counter = int(effective)
            series.last_counter_year = int(year)
            db.add(series)
        return int(effective)

    def _invoice_series_next_number_preview(self, db: Session, *, subject_id: int, series: InvoiceSeries, year: int) -> str:
        observed = self._observed_series_counter_for_year(db, subject_id=int(subject_id), series=series, year=int(year))
        try:
            last_year = int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None
        except Exception:
            last_year = None
        current = int(series.last_counter or 0) if last_year == int(year) else 0
        next_counter = max(int(current), int(observed)) + 1
        return self._format_invoice_number(series, next_counter, year=int(year))

    def _format_invoice_number(self, series: InvoiceSeries, counter: int, *, year: int | None = None) -> str:
        number_year = int(year or self._invoice_number_year())
        prefix = self._normalized_series_prefix(series.prefix, year=number_year)
        pad = max(1, min(int(series.pad_length or 0), 20))
        digits = str(int(counter)).zfill(pad)
        number = f"{prefix}{digits}"
        if len(number) > 50:
            raise ApiError(422, "invoice_number_too_long", "Číslo faktury je příliš dlouhé pro DB sloupec.")
        return number

    def _allocate_next_invoice_number(
        self,
        db: Session,
        *,
        subject_id: int,
        series_id: int,
        invoice_id: int,
        issue_date: date | None = None,
    ) -> str:
        series = db.scalar(
            select(InvoiceSeries)
            .where(InvoiceSeries.id == int(series_id))
            .where(InvoiceSeries.subject_id == int(subject_id))
            .with_for_update()
        )
        if series is None:
            raise ApiError(422, "invoice_series_not_found", "Číselná řada neexistuje.", {"series_id": int(series_id)})
        number_year = self._invoice_number_year(issue_date)
        self._sync_series_counter_for_year(db, subject_id=int(subject_id), series=series, year=int(number_year))
        try:
            last_year = int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None
        except Exception:
            last_year = None
        base_counter = 0 if last_year != number_year else int(series.last_counter or 0)
        for offset in range(1, 1001):
            next_counter = int(base_counter) + offset
            candidate = self._format_invoice_number(series, next_counter, year=number_year)
            exists = db.scalar(
                select(func.count(Invoice.id)).where(
                    Invoice.subject_id == int(subject_id),
                    Invoice.number == str(candidate),
                    Invoice.id != int(invoice_id),
                )
            )
            if int(exists or 0) == 0:
                series.last_counter = int(next_counter)
                series.last_counter_year = int(number_year)
                return candidate
        raise ApiError(409, "invoice_number_allocation_failed", "Nepodařilo se vybrat unikátní číslo faktury.")

    def _default_subject_bank_account(self, db: Session, *, subject_id: int, currency: str | None = None) -> SubjectBankAccount | None:
        rows = db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
        ).all()
        if not rows:
            return None
        normalized_currency = (str(currency or "") or "").strip().upper()
        matching_rows = [
            row for row in rows
            if normalized_currency and (str(getattr(row, "currency", "") or "").strip().upper() == normalized_currency)
        ]
        if matching_rows:
            return next((row for row in matching_rows if bool(getattr(row, "is_default", False))), matching_rows[0])
        return next((row for row in rows if bool(getattr(row, "is_default", False))), rows[0])

    def _resolve_bank_account_selection(
        self,
        db: Session,
        *,
        subject_id: int,
        requested_bank_account_id: int | None,
        currency: str | None,
    ) -> SubjectBankAccount | None:
        if requested_bank_account_id is None:
            return self._default_subject_bank_account(db, subject_id=subject_id, currency=currency)
        row = db.scalar(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.id == int(requested_bank_account_id))
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .limit(1)
        )
        if row is None:
            raise ApiError(422, "invoice_bank_account_not_found", "Vybraný účet neexistuje.", {"field": "bank_account_id", "bank_account_id": int(requested_bank_account_id)})
        return row

    def _invoice_bank_account_payload(self, invoice: Invoice, *, subject: Subject | None) -> BankAccountPayload | None:
        number = str(getattr(invoice, "bank_account_number", None) or "").strip()
        iban = str(getattr(invoice, "bank_account_iban", None) or "").strip()
        bic = str(getattr(invoice, "bank_account_bic", None) or "").strip()
        country = str(getattr(invoice, "bank_account_country", None) or "").strip() or str(getattr(subject, "country", None) or "CZ")
        label = str(getattr(invoice, "bank_account_label", None) or "").strip()
        if number or iban:
            try:
                return resolve_bank_account(account_number=number, iban=iban, bic=bic, country=country, label=label)
            except ValueError:
                return BankAccountPayload(label=label or "Bankovní účet", number=number, iban=iban, bic=bic, country=country)
        fallback_raw = str(getattr(subject, "bank_account", "") or "").strip() if subject else ""
        if not fallback_raw:
            return None
        try:
            return resolve_bank_account(account_number=fallback_raw, country=(getattr(subject, "country", None) or "CZ") if subject else "CZ", label="Hlavní účet")
        except ValueError:
            return BankAccountPayload(label="Bankovní účet", number=fallback_raw, iban="", bic="", country=(getattr(subject, "country", None) or "CZ") if subject else "CZ")

    def _apply_invoice_bank_account_snapshot(
        self,
        invoice: Invoice,
        *,
        account: SubjectBankAccount | None,
        subject: Subject | None,
        allow_subject_fallback: bool = True,
    ) -> None:
        payload: BankAccountPayload | None = None
        if account is not None:
            try:
                payload = resolve_bank_account(
                    account_number=(getattr(account, "account_number", "") or ""),
                    iban=(getattr(account, "iban", None) or ""),
                    bic=(getattr(account, "bic", None) or ""),
                    country=(getattr(account, "country", None) or "CZ"),
                    label=(getattr(account, "label", None) or ""),
                )
            except ValueError:
                payload = BankAccountPayload(
                    label=(getattr(account, "label", None) or "Bankovní účet"),
                    number=(getattr(account, "account_number", None) or ""),
                    iban=(getattr(account, "iban", None) or ""),
                    bic=(getattr(account, "bic", None) or ""),
                    country=(getattr(account, "country", None) or "CZ"),
                )
            invoice.bank_account_id = int(account.id)
        elif invoice.bank_account_id is None and invoice.bank_account_number:
            return
        elif allow_subject_fallback:
            payload = self._invoice_bank_account_payload(invoice, subject=subject)
            invoice.bank_account_id = None
        else:
            payload = None
            invoice.bank_account_id = None
        if payload is None:
            invoice.bank_account_label = None
            invoice.bank_account_number = None
            invoice.bank_account_iban = None
            invoice.bank_account_bic = None
            invoice.bank_account_country = None
            return
        invoice.bank_account_label = payload.label or None
        invoice.bank_account_number = payload.number or None
        invoice.bank_account_iban = payload.iban or None
        invoice.bank_account_bic = payload.bic or None
        invoice.bank_account_country = payload.country or None

    def _party_payload_from_subject(self, subject: Subject | None) -> dict[str, str]:
        if subject is None:
            return {"name": "", "email": "", "phone": "", "street": "", "city": "", "zip": "", "country": "CZ", "ico": "", "dic": ""}
        return {
            "name": str(subject.name or ""),
            "email": str(subject.email or ""),
            "phone": str(subject.phone or ""),
            "street": str(subject.street or ""),
            "city": str(subject.city or ""),
            "zip": str(subject.zip or ""),
            "country": str(subject.country or "CZ"),
            "ico": str(subject.ico or ""),
            "dic": str(subject.dic or ""),
        }

    def _party_payload_from_contact(self, contact: Contact | None) -> dict[str, str]:
        if contact is None:
            return {"name": "", "email": "", "phone": "", "street": "", "city": "", "zip": "", "country": "CZ", "ico": "", "dic": ""}
        return {
            "name": str(contact.name or ""),
            "email": str(contact.email or ""),
            "phone": str(contact.phone or ""),
            "street": str(contact.street or ""),
            "city": str(contact.city or ""),
            "zip": str(contact.zip or ""),
            "country": str(contact.country or "CZ"),
            "ico": str(contact.ico or ""),
            "dic": str(contact.dic or ""),
        }

    def _upsert_invoice_party(self, db: Session, *, invoice_id: int, role: str, payload: dict[str, str], sync_existing: bool = True) -> InvoiceParty:
        existing = db.scalar(
            select(InvoiceParty).where(
                InvoiceParty.invoice_id == int(invoice_id),
                InvoiceParty.role == str(role),
            )
        )
        if existing is None:
            party = InvoiceParty(invoice_id=int(invoice_id), role=str(role), **payload)
            db.add(party)
            return party
        if sync_existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        return existing

    def _sync_invoice_parties(self, db: Session, *, invoice: Invoice, subject: Subject | None, contact: Contact | None, sync_existing: bool = True) -> tuple[InvoiceParty, InvoiceParty]:
        seller = self._upsert_invoice_party(
            db,
            invoice_id=int(invoice.id),
            role="seller",
            payload=self._party_payload_from_subject(subject),
            sync_existing=sync_existing,
        )
        buyer = self._upsert_invoice_party(
            db,
            invoice_id=int(invoice.id),
            role="buyer",
            payload=self._party_payload_from_contact(contact),
            sync_existing=sync_existing,
        )
        invoice.buyer_name_cache = buyer.name or None
        invoice.buyer_registration_no_cache = buyer.ico or None
        return buyer, seller

    def _replace_invoice_items(self, db: Session, *, invoice_id: int, items_payload: list[dict[str, object]]) -> None:
        existing = db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == int(invoice_id))
            .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
        ).all()
        for item in existing:
            db.delete(item)
        db.flush()
        for idx, payload in enumerate(items_payload, start=1):
            db.add(
                InvoiceItem(
                    invoice_id=int(invoice_id),
                    description=str(payload["description"]),
                    quantity=payload["quantity"],
                    unit=self._normalize_invoice_item_unit(str(payload.get("unit") or "")),
                    unit_price_cents=int(payload["unit_price_cents"]),
                    vat_rate=payload["vat_rate"],
                    line_net_cents=int(payload["line_net_cents"]),
                    line_vat_cents=int(payload["line_vat_cents"]),
                    line_total_cents=int(payload["line_total_cents"]),
                    sort_order=idx,
                )
            )
        db.flush()

    def _recalc_invoice_total_cents(self, db: Session, *, invoice: Invoice) -> None:
        items_total = db.scalar(
            select(func.coalesce(func.sum(InvoiceItem.line_total_cents), 0)).where(InvoiceItem.invoice_id == int(invoice.id))
        )
        invoice.total_cents = int(items_total or 0) - int(getattr(invoice, "discount_cents", 0) or 0) + int(invoice.rounding_adjustment_cents or 0)

    def _request_hash(self, payload: BaseModel | dict[str, Any] | None, *, exclude_unset: bool) -> str:
        if payload is None:
            data: Any = {}
        elif isinstance(payload, BaseModel):
            data = payload.model_dump(mode="json", exclude_unset=exclude_unset)
        else:
            data = payload
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _replay_idempotent_response(
        self,
        db: Session,
        *,
        actor: ApiActor,
        request: Request,
        idempotency_key: str | None,
        request_hash: str,
    ) -> JSONResponse | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        row = db.scalar(
            select(ApiIdempotencyKey)
            .where(ApiIdempotencyKey.user_id == int(actor.user.id))
            .where(ApiIdempotencyKey.request_method == str(request.method).upper())
            .where(ApiIdempotencyKey.request_path == _request_scope_path(request))
            .where(ApiIdempotencyKey.idempotency_key == key)
            .limit(1)
        )
        if row is None:
            return None
        if str(row.request_hash or "") != str(request_hash):
            raise ApiError(409, "idempotency_key_reused", "Stejný Idempotency-Key už byl použit s jiným obsahem požadavku.", {"idempotency_key": key})
        try:
            content = json.loads(str(row.response_body_json or "{}"))
        except Exception:
            content = {}
        return JSONResponse(status_code=int(row.response_status), content=content)

    def _remember_idempotent_response(
        self,
        db: Session,
        *,
        actor: ApiActor,
        request: Request,
        idempotency_key: str | None,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        subject_id: int | None,
    ) -> None:
        key = str(idempotency_key or "").strip()
        if not key:
            return
        row = ApiIdempotencyKey(
            user_id=int(actor.user.id),
            subject_id=(int(subject_id) if subject_id is not None else None),
            request_method=str(request.method).upper(),
            request_path=_request_scope_path(request),
            idempotency_key=key,
            request_hash=str(request_hash),
            response_status=int(response_status),
            response_body_json=json.dumps(response_body, ensure_ascii=False, sort_keys=True),
        )
        db.add(row)

    def _invoice_revert_target(self, invoice: Invoice | None) -> str | None:
        if invoice is None:
            return None
        status = str(getattr(invoice, "status", "") or "").strip().lower()
        if status == "paid":
            return "sent" if getattr(invoice, "sent_at", None) is not None else "issued"
        if status == "cancelled":
            return "sent" if getattr(invoice, "sent_at", None) is not None else "issued"
        if status == "sent":
            return "issued"
        if status == "issued":
            return "draft"
        return None

    def _invoice_status_transition_error(self, *, old_status: str, new_status: str) -> str:
        if new_status == "sent":
            return "Označit jako odeslanou lze jen fakturu ve stavu 'vystavená'."
        if new_status == "paid":
            return "Označit jako zaplacenou lze jen vystavenou/odeslanou fakturu."
        if new_status == "issued":
            return "Vrátit na vystavenou lze jen odeslanou nebo zaplacenou fakturu."
        if new_status == "cancelled":
            return "Stornovat lze jen vystavenou, odeslanou nebo zaplacenou fakturu."
        if new_status == "draft":
            return "Vrátit na koncept lze jen vystavenou fakturu."
        return "Neplatný stav faktury."

    def _apply_invoice_status_transition(self, invoice: Invoice, *, new_status: str) -> tuple[bool, str | None]:
        old_status = str(getattr(invoice, "status", "") or "").strip().lower()
        target = str(new_status or "").strip().lower()
        if old_status == target:
            return False, "Stav dokladu už je nastavený."

        revert_target = self._invoice_revert_target(invoice)
        allowed_targets: set[str] = set()
        if old_status == "issued":
            allowed_targets = {"sent", "paid", "draft", "cancelled"}
        elif old_status == "sent":
            allowed_targets = {"paid", "issued", "cancelled"}
        elif old_status == "paid":
            allowed_targets = {revert_target} if revert_target else set()
            allowed_targets.add("cancelled")
        elif old_status == "cancelled":
            allowed_targets = {revert_target} if revert_target else set()

        if target not in allowed_targets:
            return False, self._invoice_status_transition_error(old_status=old_status, new_status=target)

        if target == "draft":
            invoice.status = "draft"
            invoice.number = f"DRAFT-{int(invoice.id)}"
            invoice.issued_at = None
            invoice.sent_at = None
            invoice.paid_on = None
            invoice.pdf_path = None
            invoice.pdf_hash = None
            invoice.pdf_generated_at = None
            return True, None

        if target == "issued":
            invoice.status = "issued"
            invoice.sent_at = None
            invoice.paid_on = None
            if invoice.issued_at is None:
                invoice.issued_at = utc_now()
            return True, None

        if target == "cancelled":
            invoice.status = "cancelled"
            invoice.paid_on = None
            return True, None

        if target == "sent":
            invoice.status = "sent"
            if invoice.sent_at is None:
                invoice.sent_at = utc_now()
            invoice.paid_on = None
            return True, None

        if target == "paid":
            invoice.status = "paid"
            if invoice.paid_on is None:
                invoice.paid_on = date.today()
            return True, None

        return False, "Neplatný stav dokladu."

    def _manual_payment_note(self, source: str | None = None) -> str:
        normalized = str(source or "").strip().lower()
        if normalized == "api_bulk":
            return "Ručně označeno jako zaplacené přes API hromadnou akcí"
        return "Ručně označeno jako zaplacené přes API"

    def _ensure_manual_invoice_payment(
        self,
        db: Session,
        *,
        invoice: Invoice,
        paid_on: date | None = None,
        source: str | None = None,
    ) -> Payment:
        payment = Payment(
            invoice_id=int(invoice.id),
            paid_on=paid_on or getattr(invoice, "paid_on", None) or date.today(),
            amount_cents=int(getattr(invoice, "total_cents", 0) or 0),
            note=self._manual_payment_note(source),
        )
        db.add(payment)
        db.flush()
        return payment

    def _remove_unlinked_manual_invoice_payments(self, db: Session, *, invoice: Invoice) -> int:
        payment_rows = db.scalars(
            select(Payment)
            .where(Payment.invoice_id == int(invoice.id))
            .order_by(Payment.id.asc())
        ).all()
        if not payment_rows:
            return 0
        payment_ids = [
            int(getattr(payment, "id", 0) or 0)
            for payment in payment_rows
            if getattr(payment, "id", None) is not None
        ]
        linked_payment_ids: set[int] = set()
        if payment_ids:
            linked_payment_ids = {
                int(row)
                for row in db.scalars(
                    select(BankTransaction.payment_id)
                    .where(BankTransaction.payment_id.in_(payment_ids))
                ).all()
                if row is not None
            }
        removed = 0
        for payment in payment_rows:
            payment_id = int(getattr(payment, "id", 0) or 0)
            note = str(getattr(payment, "note", "") or "")
            if payment_id in linked_payment_ids:
                continue
            if note.startswith("Ručně označeno jako zaplacené"):
                db.delete(payment)
                removed += 1
        return removed

    def _validate_payment_note(self, note: str | None) -> str | None:
        normalized = self._coalesce_optional_text(note)
        if normalized is not None and len(normalized) > 255:
            raise ApiError(422, "payment_note_too_long", "Poznámka k platbě může mít maximálně 255 znaků.")
        return normalized

    def _refresh_invoice_payment_state(self, db: Session, *, invoice: Invoice) -> None:
        remaining_payments = db.scalars(
            select(Payment)
            .where(Payment.invoice_id == int(invoice.id))
            .order_by(Payment.paid_on.desc(), Payment.id.desc())
        ).all()
        if remaining_payments:
            latest = remaining_payments[0]
            invoice.status = "paid"
            invoice.paid_on = latest.paid_on
            return
        if str(getattr(invoice, "status", "") or "").strip().lower() == "paid":
            revert_target = self._invoice_revert_target(invoice)
            if revert_target == "sent":
                invoice.status = "sent"
                invoice.paid_on = None
            elif revert_target == "issued":
                invoice.status = "issued"
                invoice.paid_on = None
            else:
                invoice.paid_on = None

    def _payment_note_for_bank_transaction(self, row: BankTransaction, *, override: str | None = None) -> str | None:
        explicit = self._validate_payment_note(override)
        if explicit is not None:
            return explicit
        provider = str(getattr(row, "provider", "") or "")
        if provider == "fio_api":
            label = "Fio API"
        elif provider == "api_manual":
            label = "API import"
        elif provider == "email_bank_raiffeisenbank_cz":
            label = "e-mail Raiffeisenbank"
        elif provider == "email_bank_csob_cz":
            label = "e-mail ČSOB"
        elif provider == "email_bank_fio_email_cz":
            label = "e-mail Fio banky"
        else:
            label = "bankovní sync"
        return f"Spárováno přes {label} ({str(getattr(row, 'external_id', '') or '').strip()})"[:255]

    def _issue_invoice_draft(self, db: Session, *, subject: Subject, invoice: Invoice) -> Invoice:
        if str(invoice.status or "") != "draft":
            raise ApiError(409, "invoice_not_draft", "Doklad už není v konceptu a nejde znovu vystavit.", {"invoice_id": int(invoice.id), "status": str(invoice.status or "")})

        contact = self._load_contact_for_subject(db, subject_id=int(invoice.subject_id), contact_id=int(invoice.contact_id))
        source_invoice = self._resolve_invoice_source(
            db,
            subject_id=int(invoice.subject_id),
            document_type=str(invoice.document_type or "invoice"),
            source_invoice_id=invoice.source_invoice_id,
            current_invoice_id=int(invoice.id),
        )
        self._validate_credit_note_amount(
            db,
            document_type=str(invoice.document_type or "invoice"),
            source_invoice=source_invoice,
            current_invoice_id=int(invoice.id),
            proposed_total_cents=int(invoice.total_cents or 0),
        )
        selected_series = self._resolve_existing_series(db, subject_id=int(invoice.subject_id), series_id=invoice.series_id)
        if selected_series is None:
            selected_series = self._resolve_invoice_series(
                db,
                subject_id=int(invoice.subject_id),
                document_type=str(invoice.document_type or "invoice"),
                requested_series_id=None,
            )
            invoice.series_id = int(selected_series.id)
        selected_bank_account: SubjectBankAccount | None = None
        if invoice.bank_account_id is not None:
            selected_bank_account = self._resolve_bank_account_selection(
                db,
                subject_id=int(invoice.subject_id),
                requested_bank_account_id=int(invoice.bank_account_id),
                currency=str(invoice.currency or subject.default_currency or "CZK"),
            )
        elif not (invoice.bank_account_number or invoice.bank_account_iban):
            selected_bank_account = self._default_subject_bank_account(
                db,
                subject_id=int(invoice.subject_id),
                currency=str(invoice.currency or subject.default_currency or "CZK"),
            )
        invoice.number = self._allocate_next_invoice_number(
            db,
            subject_id=int(invoice.subject_id),
            series_id=int(selected_series.id),
            invoice_id=int(invoice.id),
            issue_date=invoice.issue_date,
        )
        if not self._normalize_variable_symbol_field(invoice.variable_symbol):
            invoice.variable_symbol = self._contact_fixed_variable_symbol(contact) or variable_symbol_from_invoice_number(invoice.number)
        invoice.status = "issued"
        invoice.issued_at = utc_now()
        ensure_invoice_public_link(db, invoice=invoice, subject=subject)
        self._sync_invoice_parties(db, invoice=invoice, subject=subject, contact=contact, sync_existing=True)
        if selected_bank_account is not None:
            self._apply_invoice_bank_account_snapshot(invoice, account=selected_bank_account, subject=subject, allow_subject_fallback=True)
        elif not (invoice.bank_account_number or invoice.bank_account_iban):
            self._apply_invoice_bank_account_snapshot(invoice, account=None, subject=subject, allow_subject_fallback=True)
        self._recalc_invoice_total_cents(db, invoice=invoice)
        db.flush()
        return invoice

    def _link_bank_transaction_to_invoice(
        self,
        db: Session,
        *,
        row: BankTransaction,
        invoice: Invoice,
        note: str | None = None,
    ) -> Payment:
        if row.payment_id is not None or row.matched_invoice_id is not None:
            if int(row.matched_invoice_id or 0) == int(invoice.id) and row.payment_id is not None:
                payment = db.get(Payment, int(row.payment_id))
                if payment is not None:
                    invoice_refresh = self._load_invoice_for_subject(db, subject_id=int(invoice.subject_id), invoice_id=int(invoice.id))
                    return self._load_payment_for_invoice(db, invoice_id=int(invoice_refresh.id), payment_id=int(payment.id))
            raise ApiError(409, "bank_transaction_already_matched", "Bankovní transakce už je spárovaná s jiným dokladem.", {"transaction_id": int(row.id)})
        if str(getattr(row, "direction", "") or "").strip().lower() != "incoming":
            raise ApiError(409, "bank_transaction_not_incoming", "Spárovat lze jen příchozí transakci.", {"transaction_id": int(row.id)})
        if int(getattr(row, "amount_cents", 0) or 0) <= 0:
            raise ApiError(409, "bank_transaction_amount_invalid", "Spárovat lze jen kladnou transakci.", {"transaction_id": int(row.id)})
        if str(getattr(invoice, "currency", "CZK") or "CZK").upper() != str(getattr(row, "currency", "CZK") or "CZK").upper():
            raise ApiError(409, "bank_transaction_currency_mismatch", "Měna transakce neodpovídá měně dokladu.", {"transaction_id": int(row.id), "invoice_id": int(invoice.id)})
        if int(getattr(invoice, "total_cents", 0) or 0) != int(getattr(row, "amount_cents", 0) or 0):
            raise ApiError(409, "bank_transaction_amount_mismatch", "Částka transakce musí přesně odpovídat částce dokladu.", {"transaction_id": int(row.id), "invoice_id": int(invoice.id)})
        if getattr(invoice, "bank_account_id", None) is not None and int(invoice.bank_account_id) != int(row.subject_bank_account_id):
            raise ApiError(409, "bank_transaction_bank_account_mismatch", "Transakce patří k jinému bankovnímu účtu, než je na dokladu.", {"transaction_id": int(row.id), "invoice_id": int(invoice.id)})
        current_status = str(getattr(invoice, "status", "") or "").strip().lower()
        if current_status not in {"issued", "sent", "paid"}:
            raise ApiError(409, "invoice_payment_state_invalid", "Platbu lze párovat jen k vystavenému, odeslanému nebo zaplacenému dokladu.", {"invoice_id": int(invoice.id), "status": current_status})

        if current_status != "paid":
            changed, error = self._apply_invoice_status_transition(invoice, new_status="paid")
            if not changed and current_status != "paid":
                raise ApiError(409, "invoice_payment_transition_invalid", error or "Nepodařilo se označit doklad jako zaplacený.", {"invoice_id": int(invoice.id), "status": current_status})
        invoice.paid_on = row.booked_on
        payment = Payment(
            invoice_id=int(invoice.id),
            paid_on=row.booked_on,
            amount_cents=int(row.amount_cents or 0),
            note=self._payment_note_for_bank_transaction(row, override=note),
        )
        db.add(payment)
        db.flush()
        row.matched_invoice_id = int(invoice.id)
        row.payment_id = int(payment.id)
        row.matched_at = utc_now()
        db.add(row)
        db.add(invoice)
        db.flush()
        return self._load_payment_for_invoice(db, invoice_id=int(invoice.id), payment_id=int(payment.id))

    def _unlink_bank_transaction(self, db: Session, *, row: BankTransaction) -> tuple[int | None, Invoice | None]:
        payment_id = int(row.payment_id) if row.payment_id is not None else None
        invoice_id = int(row.matched_invoice_id) if row.matched_invoice_id is not None else None
        invoice: Invoice | None = None
        if invoice_id is not None:
            invoice = db.get(Invoice, invoice_id)
        payment = db.get(Payment, payment_id) if payment_id is not None else None
        row.matched_invoice_id = None
        row.payment_id = None
        row.matched_at = None
        db.add(row)
        if payment is not None:
            db.delete(payment)
        db.flush()
        if invoice is not None:
            self._refresh_invoice_payment_state(db, invoice=invoice)
            db.add(invoice)
            db.flush()
        return payment_id, invoice

    def _bank_sync_candidate_invoices(
        self,
        db: Session,
        *,
        subject_id: int,
        account_id: int,
        booked_on: date,
        amount_cents: int,
        currency: str,
        variable_symbol: str | None,
        message: str | None = None,
    ) -> list[Invoice]:
        normalized_vs = digits_only(variable_symbol)[:10]
        if not normalized_vs or amount_cents <= 0:
            normalized_vs = ""
        invoices = db.scalars(
            select(Invoice)
            .options(joinedload(Invoice.contact))
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.status.in_(["issued", "sent"]))
            .where(Invoice.document_type.in_(["invoice", "proforma"]))
            .where(Invoice.issue_date <= booked_on)
            .where(Invoice.total_cents == int(amount_cents))
            .where(func.upper(Invoice.currency) == str(currency or "CZK").upper())
            .where(or_(Invoice.bank_account_id == int(account_id), Invoice.bank_account_id.is_(None)))
            .order_by(Invoice.issue_date.desc(), Invoice.id.desc())
        ).unique().all()
        matched: list[Invoice] = []
        if normalized_vs:
            for invoice in invoices:
                if self._resolve_invoice_variable_symbol(explicit_value=invoice.variable_symbol, contact=invoice.contact) == normalized_vs:
                    matched.append(invoice)
            if matched:
                return matched
        note = re.sub(r"\s+", " ", str(message or "").strip())
        if note:
            invoice_numbers = {
                str(value).upper()
                for value in re.findall(r"\b\d{4}(?:-[A-Z]+)?-\d{4,}\b", note.upper())
            }
            if invoice_numbers:
                for invoice in invoices:
                    if str(getattr(invoice, "number", "") or "").strip().upper() in invoice_numbers:
                        matched.append(invoice)
        return matched

    def _retry_existing_unmatched_bank_transactions(self, db: Session, *, account: SubjectBankAccount) -> tuple[int, int]:
        rows = db.scalars(
            select(BankTransaction)
            .options(joinedload(BankTransaction.matched_invoice), joinedload(BankTransaction.payment), joinedload(BankTransaction.bank_account))
            .where(BankTransaction.subject_bank_account_id == int(account.id))
            .where(BankTransaction.matched_invoice_id.is_(None))
            .where(BankTransaction.direction == "incoming")
            .where(BankTransaction.amount_cents > 0)
            .order_by(BankTransaction.booked_on.desc(), BankTransaction.id.desc())
        ).unique().all()
        matched = 0
        for row in rows:
            candidates = self._bank_sync_candidate_invoices(
                db,
                subject_id=int(account.subject_id),
                account_id=int(account.id),
                booked_on=row.booked_on,
                amount_cents=int(row.amount_cents or 0),
                currency=str(row.currency or "CZK"),
                variable_symbol=row.variable_symbol,
                message=row.message,
            )
            if len(candidates) == 1:
                self._link_bank_transaction_to_invoice(db, row=row, invoice=candidates[0])
                matched += 1
        return len(rows), matched

    def _normalize_payment_sync_provider(self, value: str | None) -> str:
        normalized = str(value or "none").strip().lower() or "none"
        if normalized not in VALID_PAYMENT_SYNC_PROVIDERS:
            return "none"
        return normalized

    def _normalize_payment_sync_email_parser(self, value: str | None) -> str:
        normalized = str(value or "pending").strip().lower() or "pending"
        if normalized not in VALID_EMAIL_BANK_PARSERS:
            return "pending"
        return normalized

    def _payment_sync_email_defaults(self, parser_name: str | None) -> dict[str, str]:
        parser = self._normalize_payment_sync_email_parser(parser_name)
        defaults = EMAIL_BANK_PARSER_DEFAULTS.get(parser, {})
        return {
            "sender": str(defaults.get("sender") or "").strip().lower(),
            "subject": str(defaults.get("subject") or "").strip(),
            "description": str(defaults.get("description") or "").strip(),
        }

    def _payment_sync_alert_email_for_localpart(self, localpart: str | None) -> str:
        clean_localpart = str(localpart or "").strip()
        clean_domain = str(getattr(self.settings, "payment_sync_alert_domain", "") or "").strip().lower()
        if not clean_localpart or not clean_domain:
            return ""
        return f"{clean_localpart}@{clean_domain}"

    def _payment_sync_alert_email_for_account(self, account: SubjectBankAccount | None) -> str:
        if account is None:
            return ""
        return self._payment_sync_alert_email_for_localpart(getattr(account, "payment_sync_alert_localpart", None))

    def _decode_fio_api_token(self, value: str | None) -> str:
        return str(
            decrypt_secret(
                value,
                secret_key=str(self.settings.data_encryption_key or ""),
                purpose="fio-api-token",
            )
            or ""
        ).strip()

    def _payment_sync_date_window(self, account: SubjectBankAccount, *, today_local: date | None = None) -> tuple[date, date]:
        today_value = today_local or date.today()
        cursor_date = getattr(account, "payment_sync_cursor_date", None)
        if cursor_date is None:
            return today_value, today_value
        return max(cursor_date - timedelta(days=BANK_SYNC_OVERLAP_DAYS), today_value - timedelta(days=365)), today_value

    def _body_preview(self, value: str | None, *, limit: int = 200) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return None
        max_len = max(32, min(int(limit or 200), 1000))
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    def _json_dumps_safe(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return None

    def _sanitize_bank_email_body(self, value: str | None) -> str:
        text = str(value or "")
        if not text.strip():
            raise ApiError(422, "bank_email_body_required", "Tělo bankovního e-mailu je povinné.")
        if len(text) > 20_000:
            raise ApiError(422, "bank_email_body_too_large", "Tělo bankovního e-mailu je příliš dlouhé.")
        return text.strip()

    def _build_api_email_uid(self, *, account_id: int, payload: BankIncomingEmailImportRequest) -> str:
        digest = hashlib.sha1(
            json.dumps(
                {
                    "account_id": int(account_id),
                    "external_message_id": str(payload.external_message_id or "").strip(),
                    "received_at": payload.received_at.isoformat() if payload.received_at else None,
                    "from_email": str(payload.from_email or "").strip().lower(),
                    "subject": str(payload.subject or "").strip(),
                    "body_text": self._sanitize_bank_email_body(payload.body_text),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8", errors="ignore"),
            usedforsecurity=False,
        ).hexdigest()
        return f"api-{digest}"

    def _manual_imported_bank_transaction(
        self,
        item: BankTransactionImportItemRequest,
        *,
        account: SubjectBankAccount,
    ) -> ImportedBankTransaction:
        external_id = str(item.external_id or "").strip()
        if not external_id:
            raise ApiError(422, "bank_transaction_external_id_required", "external_id transakce je povinné.")
        if len(external_id) > 128:
            raise ApiError(422, "bank_transaction_external_id_too_long", "external_id transakce je příliš dlouhé.")
        direction = str(item.direction or "incoming").strip().lower() or "incoming"
        if direction not in {"incoming", "outgoing"}:
            raise ApiError(422, "bank_transaction_direction_invalid", "direction musí být incoming nebo outgoing.")
        amount_cents = parse_money_to_signed_cents(item.amount)
        if direction == "incoming" and amount_cents < 0:
            amount_cents = abs(amount_cents)
        if direction == "outgoing" and amount_cents > 0:
            amount_cents = -amount_cents
        currency = str(item.currency or getattr(account, "currency", "CZK") or "CZK").strip().upper()[:3] or "CZK"
        return ImportedBankTransaction(
            provider="api_manual",
            external_id=external_id,
            booked_on=item.booked_on,
            amount_cents=int(amount_cents),
            currency=currency,
            direction=direction,
            variable_symbol=(digits_only(item.variable_symbol)[:10] or None),
            constant_symbol=(digits_only(item.constant_symbol)[:4] or None),
            specific_symbol=(digits_only(item.specific_symbol)[:10] or None),
            counterparty_account=self._none_str(item.counterparty_account),
            counterparty_name=self._none_str(item.counterparty_name),
            message=self._none_str(item.message),
            raw_payload={
                "source": "api_manual",
                "external_id": external_id,
            },
        )

    def _import_bank_transaction_row(
        self,
        db: Session,
        *,
        account: SubjectBankAccount,
        imported: ImportedBankTransaction,
        auto_pair: bool,
    ) -> tuple[BankTransaction, bool, bool]:
        def _limit_optional(value: str | None, limit: int) -> str | None:
            text = normalize_spaces(str(value or ""))
            if not text:
                return None
            return text[:limit]

        existing = db.scalar(
            select(BankTransaction)
            .where(BankTransaction.subject_bank_account_id == int(account.id))
            .where(BankTransaction.provider == imported.provider)
            .where(BankTransaction.external_id == imported.external_id)
            .limit(1)
        )
        if existing is not None:
            return self._load_bank_transaction_for_subject(db, subject_id=int(account.subject_id), transaction_id=int(existing.id)), False, bool(existing.matched_invoice_id)

        row = BankTransaction(
            subject_bank_account_id=int(account.id),
            provider=imported.provider,
            external_id=imported.external_id,
            booked_on=imported.booked_on,
            amount_cents=int(imported.amount_cents),
            currency=str(imported.currency or getattr(account, "currency", "CZK") or "CZK").upper()[:3] or "CZK",
            direction=str(imported.direction or "incoming").strip().lower() or "incoming",
            variable_symbol=imported.variable_symbol,
            constant_symbol=imported.constant_symbol,
            specific_symbol=imported.specific_symbol,
            counterparty_account=_limit_optional(imported.counterparty_account, 255),
            counterparty_name=_limit_optional(imported.counterparty_name, 255),
            message=(str(imported.message or "").strip() or None),
            raw_payload_json=self._json_dumps_safe(imported.raw_payload),
        )
        db.add(row)
        db.flush()

        matched = False
        if auto_pair and row.direction == "incoming" and int(row.amount_cents or 0) > 0:
            candidates = self._bank_sync_candidate_invoices(
                db,
                subject_id=int(account.subject_id),
                account_id=int(account.id),
                booked_on=row.booked_on,
                amount_cents=int(row.amount_cents or 0),
                currency=str(row.currency or getattr(account, "currency", "CZK") or "CZK"),
                variable_symbol=row.variable_symbol,
                message=row.message,
            )
            if len(candidates) == 1:
                self._link_bank_transaction_to_invoice(db, row=row, invoice=candidates[0])
                matched = True
        row = self._load_bank_transaction_for_subject(db, subject_id=int(account.subject_id), transaction_id=int(row.id))
        return row, True, matched

    def _parse_email_bank_transaction(
        self,
        imported: ImportedBankEmail,
        *,
        parser_name: str | None,
    ) -> ImportedBankTransaction | None:
        parser = self._normalize_payment_sync_email_parser(parser_name)
        if parser == "pending":
            return None
        if parser == "csas_cz":
            return parse_csas_cz_email(imported)
        if parser == "csob_cz":
            return parse_csob_cz_email(imported)
        if parser == "fio_email_cz":
            return parse_fio_email_cz(imported)
        if parser == "raiffeisenbank_cz":
            return parse_raiffeisenbank_cz_email(imported)
        raise ApiError(422, "bank_email_parser_invalid", "Zvolený parser bankovních e-mailů zatím neumíme zpracovat.")

    def _rehydrate_imported_email(self, row: BankIncomingEmail) -> ImportedBankEmail:
        raw_headers: dict[str, str] = {}
        raw_headers_json = str(getattr(row, "raw_headers_json", "") or "").strip()
        if raw_headers_json:
            try:
                loaded = json.loads(raw_headers_json)
                if isinstance(loaded, dict):
                    raw_headers = {str(key): str(value) for key, value in loaded.items()}
            except Exception:
                raw_headers = {}
        return ImportedBankEmail(
            provider=str(getattr(row, "provider", "email_bank") or "email_bank"),
            imap_uid=str(getattr(row, "imap_uid", "") or ""),
            external_message_id=(str(getattr(row, "external_message_id", "") or "").strip() or None),
            received_at=getattr(row, "received_at", None),
            from_email=(str(getattr(row, "from_email", "") or "").strip().lower() or None),
            subject=(str(getattr(row, "subject", "") or "").strip() or None),
            body_text=(str(getattr(row, "body_text", "") or "").strip() or None),
            raw_headers=raw_headers,
        )

    def _process_bank_incoming_email_row(
        self,
        db: Session,
        *,
        account: SubjectBankAccount,
        email_row: BankIncomingEmail,
        auto_pair: bool,
        parser_name: str | None,
    ) -> tuple[str, bool, BankTransaction | None]:
        imported = self._rehydrate_imported_email(email_row)
        try:
            parsed = self._parse_email_bank_transaction(imported, parser_name=parser_name)
        except BankSyncError as exc:
            email_row.processing_status = "parse_failed"
            email_row.processing_note = str(exc)
            db.add(email_row)
            db.flush()
            return "parse_failed", False, None
        if parsed is None:
            email_row.processing_status = "stored"
            email_row.processing_note = "Uložené bez parseru bankovních e-mailů."
            db.add(email_row)
            db.flush()
            return "stored", False, None

        row, was_imported, matched = self._import_bank_transaction_row(
            db,
            account=account,
            imported=parsed,
            auto_pair=auto_pair,
        )
        email_row.matched_bank_transaction_id = int(row.id)
        if matched:
            email_row.processing_status = "matched"
            email_row.processing_note = "E-mail byl rozpoznán a platba spárována s fakturou."
        else:
            email_row.processing_status = "parsed_unmatched"
            email_row.processing_note = "E-mail byl rozpoznán, ale platbu se nepodařilo jednoznačně spárovat s fakturou."
        db.add(email_row)
        db.flush()
        return ("imported" if was_imported else "skipped_existing"), matched, row

    def _retry_existing_bank_emails(
        self,
        db: Session,
        *,
        account: SubjectBankAccount,
        auto_pair: bool,
        parser_name: str | None,
    ) -> dict[str, int]:
        stats = {"imported": 0, "matched": 0, "unmatched": 0, "skipped_existing": 0}
        parser = self._normalize_payment_sync_email_parser(parser_name)
        if parser == "pending":
            return stats
        rows = db.scalars(
            select(BankIncomingEmail)
            .where(BankIncomingEmail.subject_bank_account_id == int(account.id))
            .where(BankIncomingEmail.processing_status.in_(["stored", "parse_failed"]))
            .order_by(BankIncomingEmail.received_at.asc(), BankIncomingEmail.id.asc())
        ).all()
        for row in rows:
            outcome, matched, _transaction = self._process_bank_incoming_email_row(
                db,
                account=account,
                email_row=row,
                auto_pair=auto_pair,
                parser_name=parser,
            )
            if outcome == "imported":
                stats["imported"] += 1
            elif outcome == "skipped_existing":
                stats["skipped_existing"] += 1
            else:
                stats["unmatched"] += 1
            if matched:
                stats["matched"] += 1
            elif outcome in {"imported", "stored"}:
                stats["unmatched"] += 1
        return stats

    def _store_api_bank_incoming_email(
        self,
        db: Session,
        *,
        account: SubjectBankAccount,
        payload: BankIncomingEmailImportRequest,
    ) -> BankIncomingEmail:
        body_text = self._sanitize_bank_email_body(payload.body_text)
        from_email = str(payload.from_email or "").strip().lower() or None
        if from_email and not looks_like_email(from_email):
            raise ApiError(422, "bank_email_from_invalid", "from_email není platná e-mailová adresa.")
        synthetic_uid = self._build_api_email_uid(account_id=int(account.id), payload=payload)
        existing = db.scalar(
            select(BankIncomingEmail)
            .where(BankIncomingEmail.subject_bank_account_id == int(account.id))
            .where(BankIncomingEmail.provider == "email_bank_api")
            .where(BankIncomingEmail.imap_uid == synthetic_uid)
            .limit(1)
        )
        if existing is not None:
            return self._load_bank_incoming_email_for_subject(db, subject_id=int(account.subject_id), email_id=int(existing.id))

        email_row = BankIncomingEmail(
            subject_bank_account_id=int(account.id),
            provider="email_bank_api",
            imap_uid=synthetic_uid,
            external_message_id=self._none_str(payload.external_message_id),
            received_at=(as_utc_aware(payload.received_at) if payload.received_at is not None else utc_now()),
            from_email=from_email,
            subject=self._none_str(payload.subject),
            body_text=body_text,
            raw_headers_json=self._json_dumps_safe({}),
            processing_status="stored",
            processing_note="E-mail uložený přes API pro párování bankovní platby.",
        )
        db.add(email_row)
        db.flush()
        return self._load_bank_incoming_email_for_subject(db, subject_id=int(account.subject_id), email_id=int(email_row.id))

    def _sync_subject_bank_account_email(
        self,
        db: Session,
        *,
        account: SubjectBankAccount,
    ) -> BankSyncRunAccountModel:
        imap_host = str(getattr(self.settings, "payment_sync_imap_host", "") or "").strip()
        imap_username = str(getattr(self.settings, "payment_sync_imap_username", "") or "").strip()
        imap_password = str(getattr(self.settings, "payment_sync_imap_password", "") or "")
        parser_name = self._normalize_payment_sync_email_parser(getattr(account, "payment_sync_email_parser", None))
        auto_pair = bool(getattr(account, "payment_sync_auto_pair", True))
        result = {
            "bank_account_id": int(account.id),
            "provider": "email_bank",
            "fetched": 0,
            "imported": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped_existing": 0,
            "baseline_seeded": False,
            "errors": [],
        }
        if not imap_host or not imap_username or not imap_password:
            account.payment_sync_last_error = "IMAP schránka pro bankovní notifikace zatím není nastavená."
            db.add(account)
            result["errors"].append(str(account.payment_sync_last_error))
            return BankSyncRunAccountModel(**result)

        sender_filter = str(getattr(account, "payment_sync_email_sender_filter", "") or "").strip().lower() or self._payment_sync_email_defaults(parser_name).get("sender", "")
        subject_filter = str(getattr(account, "payment_sync_email_subject_filter", "") or "").strip().lower() or self._payment_sync_email_defaults(parser_name).get("subject", "").lower()
        recipient_filter = self._payment_sync_alert_email_for_account(account).strip().lower()
        previous_last_email_uid = str(getattr(account, "payment_sync_last_email_uid", "") or "").strip()

        try:
            imported_emails = fetch_imap_bank_emails(
                host=imap_host,
                port=int(getattr(self.settings, "payment_sync_imap_port", 993) or 993),
                username=imap_username,
                password=imap_password,
                mailbox=str(getattr(self.settings, "payment_sync_imap_mailbox", "INBOX") or "INBOX"),
                use_ssl=bool(getattr(self.settings, "payment_sync_imap_use_ssl", True)),
                since_uid=previous_last_email_uid or None,
            )
        except BankSyncError as exc:
            logging.getLogger("fakturek").error(
                "API IMAP bank sync failed for bank account %s (error_type=%s)",
                getattr(account, "id", "?"),
                type(exc).__name__,
            )
            account.payment_sync_last_error = safe_bank_sync_error_message(exc)
            db.add(account)
            result["errors"].append(account.payment_sync_last_error)
            return BankSyncRunAccountModel(**result)

        result["fetched"] = len(imported_emails)
        highest_uid = previous_last_email_uid
        if not previous_last_email_uid and imported_emails:
            for imported in imported_emails:
                if str(imported.imap_uid or "").strip() and (not highest_uid or (highest_uid.isdigit() and imported.imap_uid.isdigit() and int(imported.imap_uid) > int(highest_uid))):
                    highest_uid = str(imported.imap_uid)
            if highest_uid:
                account.payment_sync_last_email_uid = highest_uid
            account.payment_sync_last_success_at = utc_now()
            account.payment_sync_last_error = None
            db.add(account)
            result["baseline_seeded"] = True
            return BankSyncRunAccountModel(**result)

        for imported in imported_emails:
            if str(imported.imap_uid or "").strip() and (not highest_uid or (highest_uid.isdigit() and imported.imap_uid.isdigit() and int(imported.imap_uid) > int(highest_uid))):
                highest_uid = str(imported.imap_uid)
            from_email = str(imported.from_email or "").strip().lower()
            subject_line = str(imported.subject or "").strip().lower()
            recipients = [item.strip().lower() for item in extract_bank_email_recipients(imported)]
            if recipient_filter and recipients and recipient_filter not in recipients:
                continue
            if sender_filter and from_email != sender_filter:
                continue
            if subject_filter and subject_filter not in subject_line:
                continue

            existing = db.scalar(
                select(BankIncomingEmail)
                .where(BankIncomingEmail.subject_bank_account_id == int(account.id))
                .where(BankIncomingEmail.provider == imported.provider)
                .where(BankIncomingEmail.imap_uid == imported.imap_uid)
                .limit(1)
            )
            if existing is not None:
                result["skipped_existing"] += 1
                continue

            email_row = BankIncomingEmail(
                subject_bank_account_id=int(account.id),
                provider=imported.provider,
                imap_uid=str(imported.imap_uid or ""),
                external_message_id=imported.external_message_id,
                received_at=imported.received_at,
                from_email=imported.from_email,
                subject=imported.subject,
                body_text=imported.body_text,
                raw_headers_json=self._json_dumps_safe(imported.raw_headers),
                processing_status="stored",
                processing_note="E-mail uložený pro párování bankovní platby.",
            )
            db.add(email_row)
            db.flush()
            outcome, matched, _transaction = self._process_bank_incoming_email_row(
                db,
                account=account,
                email_row=email_row,
                auto_pair=auto_pair,
                parser_name=parser_name,
            )
            if outcome == "imported":
                result["imported"] += 1
            elif outcome == "skipped_existing":
                result["skipped_existing"] += 1
            else:
                result["unmatched"] += 1
            if matched:
                result["matched"] += 1
            elif outcome in {"imported", "stored"}:
                result["unmatched"] += 1

        retry_stats = self._retry_existing_bank_emails(
            db,
            account=account,
            auto_pair=auto_pair,
            parser_name=parser_name,
        )
        for key, value in retry_stats.items():
            result[key] = int(result.get(key) or 0) + int(value)
        if auto_pair:
            _inspected, retry_matched = self._retry_existing_unmatched_bank_transactions(db, account=account)
            result["matched"] += retry_matched

        if highest_uid:
            account.payment_sync_last_email_uid = highest_uid
        account.payment_sync_last_success_at = utc_now()
        account.payment_sync_last_error = None
        db.add(account)
        return BankSyncRunAccountModel(**result)

    def _sync_subject_bank_account(self, db: Session, *, account: SubjectBankAccount) -> BankSyncRunAccountModel:
        provider = self._normalize_payment_sync_provider(getattr(account, "payment_sync_provider", None))
        enabled = bool(getattr(account, "payment_sync_enabled", False))
        now_utc = utc_now()
        account.payment_sync_last_checked_at = now_utc
        db.add(account)

        result = {
            "bank_account_id": int(account.id),
            "provider": provider,
            "fetched": 0,
            "imported": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped_existing": 0,
            "baseline_seeded": False,
            "errors": [],
        }

        if not enabled or provider == "none":
            account.payment_sync_last_error = None
            return BankSyncRunAccountModel(**result)
        if provider == "email_bank":
            return self._sync_subject_bank_account_email(db, account=account)
        if provider != "fio_api":
            account.payment_sync_last_error = "Tento provider zatím neumíme synchronizovat."
            db.add(account)
            result["errors"].append(str(account.payment_sync_last_error))
            return BankSyncRunAccountModel(**result)

        token = self._decode_fio_api_token(getattr(account, "fio_api_token", None))
        if not token:
            account.payment_sync_last_error = "Chybí Fio API token."
            db.add(account)
            result["errors"].append(str(account.payment_sync_last_error))
            return BankSyncRunAccountModel(**result)

        date_from, date_to = self._payment_sync_date_window(account)
        try:
            imported_transactions = fetch_fio_transactions(
                token,
                date_from=date_from,
                date_to=date_to,
                base_url=str(getattr(self.settings, "fio_api_base_url", "") or ""),
                timeout_seconds=float(getattr(self.settings, "fio_timeout_seconds", 10.0) or 10.0),
            )
        except BankSyncError as exc:
            logging.getLogger("fakturek").error(
                "API Fio bank sync failed for bank account %s (error_type=%s)",
                getattr(account, "id", "?"),
                type(exc).__name__,
            )
            account.payment_sync_last_error = safe_bank_sync_error_message(exc)
            db.add(account)
            result["errors"].append(account.payment_sync_last_error)
            if bool(getattr(account, "payment_sync_auto_pair", True)):
                _inspected, retry_matched = self._retry_existing_unmatched_bank_transactions(db, account=account)
                result["matched"] += retry_matched
            return BankSyncRunAccountModel(**result)

        result["fetched"] = len(imported_transactions)
        newest_booked_on = date_from
        auto_pair = bool(getattr(account, "payment_sync_auto_pair", True))
        for imported in imported_transactions:
            if imported.booked_on > newest_booked_on:
                newest_booked_on = imported.booked_on
            if imported.direction != "incoming" or int(imported.amount_cents) <= 0:
                continue
            _row, was_imported, matched = self._import_bank_transaction_row(
                db,
                account=account,
                imported=imported,
                auto_pair=auto_pair,
            )
            if not was_imported:
                result["skipped_existing"] += 1
                continue
            result["imported"] += 1
            if matched:
                result["matched"] += 1
            else:
                result["unmatched"] += 1
        if auto_pair:
            _inspected, retry_matched = self._retry_existing_unmatched_bank_transactions(db, account=account)
            result["matched"] += retry_matched

        account.payment_sync_cursor_date = newest_booked_on if imported_transactions else date_to
        account.payment_sync_last_success_at = now_utc
        account.payment_sync_last_error = None
        db.add(account)
        return BankSyncRunAccountModel(**result)

    def _run_bank_sync_for_subject(self, db: Session, *, subject_id: int) -> BankSyncRunResponse:
        accounts = db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
        ).all()
        summary = {
            "subject_id": int(subject_id),
            "fetched": 0,
            "imported": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped_existing": 0,
            "baseline_seeded": False,
            "errors": [],
            "accounts": [],
        }
        for account in accounts:
            account_result = self._sync_subject_bank_account(db, account=account)
            summary["accounts"].append(account_result)
            summary["fetched"] += int(account_result.fetched)
            summary["imported"] += int(account_result.imported)
            summary["matched"] += int(account_result.matched)
            summary["unmatched"] += int(account_result.unmatched)
            summary["skipped_existing"] += int(account_result.skipped_existing)
            if account_result.baseline_seeded:
                summary["baseline_seeded"] = True
            summary["errors"].extend(list(account_result.errors or []))
        return BankSyncRunResponse(**summary)

    @staticmethod
    def _none_str(value: Any) -> str | None:
        raw = str(value or "").strip()
        return raw or None

    @staticmethod
    def _money(cents: int | None) -> str:
        value = Decimal(int(cents or 0)) / Decimal("100")
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    @staticmethod
    def _decimal(value: Decimal | int | float | str | None) -> str:
        if value is None:
            dec = Decimal("0.00")
        elif isinstance(value, Decimal):
            dec = value
        else:
            dec = Decimal(str(value))
        return format(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    @staticmethod
    def _d(value: date | None) -> str | None:
        if value is None:
            return None
        return value.isoformat()

    @staticmethod
    def _dt(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.replace(microsecond=0).isoformat() + "Z"


def create_api_v1_app(*, settings: Settings) -> FastAPI:
    return ApiV1Builder(settings).app

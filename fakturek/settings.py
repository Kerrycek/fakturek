from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse


def _getenv(name: str, default: str | None = None) -> str | None:
    """Small wrapper to make env reads easy to grep."""
    return os.getenv(name, default)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _require(name: str) -> str:
    v = _getenv(name)
    if v is None or not v.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v.strip()


@dataclass(frozen=True)
class Settings:
    """Application settings.

    Keep this small in early phases; grow as needed.
    """

    app_env: str
    debug: bool
    secret_key: str
    signup_token_key: str
    public_link_hmac_key: str
    data_encryption_key: str
    database_url: str

    # Auth/session
    auth_required: bool
    signup_enabled: bool
    setup_token: str | None
    internal_job_token: str | None
    trusted_proxy_ips: tuple[str, ...]

    # Issuer (seller) profile for printed invoices.
    # Keep optional and environment-driven for now.
    issuer_name: str
    issuer_email: str
    issuer_phone: str
    issuer_street: str
    issuer_city: str
    issuer_zip: str
    issuer_country: str
    issuer_ico: str
    issuer_dic: str
    issuer_bank_account: str

    # Company lookup (ARES / future providers)
    ares_base_url: str
    ares_timeout_seconds: float
    company_lookup_cache_ttl_days: int

    # Company lookup (SK)
    sk_rpo_base_url: str
    sk_rpo_timeout_seconds: float
    sk_orsr_base_url: str
    sk_orsr_timeout_seconds: float

    # PDF storage (phase-20)
    # Directory for persisted *issued* PDFs. If relative, it's resolved against
    # the project root.
    pdf_storage_dir: str

    # Public invoice (phase-21)
    # Optional externally visible base URL used for public invoice links.
    public_base_url: str
    app_base_url: str
    # Simple in-memory rate limit for public invoice endpoints.
    public_rate_limit_max: int
    public_rate_limit_window_seconds: int

    # SMTP email (phase-22)
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    # If True, connect using implicit TLS (SMTPS, typically port 465).
    smtp_use_tls: bool
    # If True, issue STARTTLS after connecting (typically port 587).
    smtp_use_starttls: bool
    smtp_timeout_seconds: float
    # Optional override for the From header.
    smtp_from_email: str
    smtp_from_name: str

    # Import (phase-24)
    # Directory for uploaded import files (kept outside webroot).
    import_storage_dir: str
    # Max upload size (MB) for import files.
    import_max_upload_mb: int

    # Bank sync (phase-55)
    fio_api_base_url: str
    fio_timeout_seconds: float
    payment_sync_imap_host: str
    payment_sync_imap_port: int
    payment_sync_imap_username: str | None
    payment_sync_imap_password: str | None
    payment_sync_imap_mailbox: str
    payment_sync_imap_use_ssl: bool
    payment_sync_alert_domain: str | None

    # ------------------------------------------------------------------
    # Security hardening (phase-29)
    # ------------------------------------------------------------------
    # Rate limit for login attempts. Limits repeated login POSTs from the same
    # client to mitigate brute-force attacks. Defaults are conservative for
    # development/testing and can be overridden via environment variables
    # LOGIN_RATE_LIMIT_MAX and LOGIN_RATE_LIMIT_WINDOW_SECONDS.
    login_rate_limit_max: int
    login_rate_limit_window_seconds: int

    # API v1 rate limiting. Applies per Bearer token to authenticated API
    # requests. It is intentionally simple and in-memory, mirroring the public
    # and login limiters.
    api_rate_limit_max: int
    api_rate_limit_window_seconds: int
    api_monthly_quota_max: int

    # CSRF protection toggle. When enabled, mutating requests (POST/PUT/PATCH/DELETE)
    # must include a CSRF token that matches the value stored in the session.
    # In production this is enabled by default; in development it can be
    # disabled for convenience by setting CSRF_ENABLED=0.
    csrf_enabled: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings from environment with sane defaults for development."""

    app_env = (_getenv("APP_ENV", "dev") or "dev").strip().lower()
    debug = _parse_bool(_getenv("DEBUG"), default=(app_env != "prod"))

    secret_key = (
        _getenv("SESSION_SIGNING_KEY", _getenv("SECRET_KEY", "CHANGE_ME_IN_PROD")) or ""
    ).strip()
    signup_token_key = (_getenv("SIGNUP_TOKEN_KEY") or "").strip()
    public_link_hmac_key = (_getenv("PUBLIC_LINK_HMAC_KEY") or "").strip()
    data_encryption_key = (_getenv("DATA_ENCRYPTION_KEY") or "").strip()

    # Auth is implemented in phase-13 but is kept opt-in for development.
    # In production we default to requiring auth.
    auth_required = _parse_bool(_getenv("AUTH_REQUIRED"), default=(app_env == "prod"))

    # Public self-service signup is optional for self-hosted installations.
    signup_enabled = _parse_bool(_getenv("SIGNUP_ENABLED"), default=(app_env != "prod"))

    # Optional token to protect the initial /setup flow in production.
    setup_token = (_getenv("SETUP_TOKEN") or "").strip() or None
    internal_job_token = (_getenv("INTERNAL_JOB_TOKEN") or "").strip() or None
    trusted_proxy_ips_raw = (_getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1,localhost") or "").strip()
    trusted_proxy_ips = tuple(
        part.strip().lower()
        for part in trusted_proxy_ips_raw.split(",")
        if part.strip()
    ) or ("127.0.0.1", "::1", "localhost")

    # MySQL/MariaDB DSN. For local dev with docker compose, use the service name "db".
    database_url = (
        _getenv(
            "DATABASE_URL",
            "mysql+pymysql://fakturek:fakturek@127.0.0.1:3306/fakturek?charset=utf8mb4",
        )
        or ""
    ).strip()

    def _validate_secret(name: str, value: str, *, min_len: int = 32) -> None:
        lowered = value.strip().lower()
        placeholders = {
            "change_me_in_prod",
            "change-me-in-production",
            "changeme",
            "change-me",
            "example",
            "secret",
            "password",
            "fakturek",
            "root",
        }
        if (
            not value
            or len(value) < min_len
            or lowered in placeholders
            or "change-me" in lowered
            or "change_me" in lowered
        ):
            raise RuntimeError(
                f"{name} must be a unique random secret with at least {min_len} characters"
            )

    if app_env == "prod":
        if debug:
            raise RuntimeError("DEBUG must be disabled in production")
        if not auth_required:
            raise RuntimeError("AUTH_REQUIRED must be enabled in production")
        secret_values = [
            ("SESSION_SIGNING_KEY", secret_key),
            ("SIGNUP_TOKEN_KEY", signup_token_key),
            ("PUBLIC_LINK_HMAC_KEY", public_link_hmac_key),
            ("DATA_ENCRYPTION_KEY", data_encryption_key),
            ("INTERNAL_JOB_TOKEN", internal_job_token or ""),
        ]
        for name, value in secret_values:
            _validate_secret(name, value)
        if setup_token:
            _validate_secret("SETUP_TOKEN", setup_token)
            secret_values.append(("SETUP_TOKEN", setup_token))
        values = [value for _, value in secret_values]
        if len(values) != len(set(values)):
            raise RuntimeError("Security secrets must be different from each other")

    issuer_name = (_getenv("ISSUER_NAME", "") or "").strip()
    issuer_email = (_getenv("ISSUER_EMAIL", "") or "").strip()
    issuer_phone = (_getenv("ISSUER_PHONE", "") or "").strip()
    issuer_street = (_getenv("ISSUER_STREET", "") or "").strip()
    issuer_city = (_getenv("ISSUER_CITY", "") or "").strip()
    issuer_zip = (_getenv("ISSUER_ZIP", "") or "").strip()
    issuer_country = (_getenv("ISSUER_COUNTRY", "CZ") or "").strip()
    issuer_ico = (_getenv("ISSUER_ICO", "") or "").strip()
    issuer_dic = (_getenv("ISSUER_DIC", "") or "").strip()
    issuer_bank_account = (_getenv("ISSUER_BANK_ACCOUNT", "") or "").strip()

    # Company lookup (CZ: ARES). Keep configurable for deployments.
    ares_base_url = (
        _getenv(
            "ARES_BASE_URL",
            "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty",
        )
        or ""
    ).strip().rstrip("/")

    # Network timeout for ARES requests.
    try:
        ares_timeout_seconds = float((_getenv("ARES_TIMEOUT_SECONDS", "5") or "5").strip())
    except ValueError:
        ares_timeout_seconds = 5.0

    # Cache TTL for company lookup responses.
    try:
        company_lookup_cache_ttl_days = int(
            (_getenv("COMPANY_LOOKUP_CACHE_TTL_DAYS", "30") or "30").strip()
        )
    except ValueError:
        company_lookup_cache_ttl_days = 30

    # Company lookup (SK: RPO + ORSR fallback)
    sk_rpo_base_url = (
        _getenv(
            "SK_RPO_BASE_URL",
            "https://api.statistics.sk/rpo/v1",
        )
        or ""
    ).strip().rstrip("/")

    try:
        sk_rpo_timeout_seconds = float((_getenv("SK_RPO_TIMEOUT_SECONDS", "5") or "5").strip())
    except ValueError:
        sk_rpo_timeout_seconds = 5.0

    sk_orsr_base_url = (
        _getenv(
            "SK_ORSR_BASE_URL",
            "https://www.orsr.sk",
        )
        or ""
    ).strip().rstrip("/")

    try:
        sk_orsr_timeout_seconds = float((_getenv("SK_ORSR_TIMEOUT_SECONDS", "5") or "5").strip())
    except ValueError:
        sk_orsr_timeout_seconds = 5.0

    # PDF storage (phase-20)
    pdf_storage_dir = (_getenv("PDF_STORAGE_DIR", "var/pdfs") or "var/pdfs").strip()

    # Public invoice (phase-21)
    public_base_url = (_getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    app_base_url = (_getenv("APP_BASE_URL", public_base_url) or public_base_url or "").strip().rstrip("/")
    if app_env == "prod":
        for name, value in (
            ("PUBLIC_BASE_URL", public_base_url),
            ("APP_BASE_URL", app_base_url),
        ):
            parsed = urlparse(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise RuntimeError(f"{name} must be a canonical HTTPS origin in production")

    try:
        public_rate_limit_max = int((_getenv("PUBLIC_RATE_LIMIT_MAX", "120") or "120").strip())
    except ValueError:
        public_rate_limit_max = 120

    try:
        public_rate_limit_window_seconds = int(
            (_getenv("PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60") or "60").strip()
        )
    except ValueError:
        public_rate_limit_window_seconds = 60

    # SMTP email (phase-22)
    smtp_host = (_getenv("SMTP_HOST", "") or "").strip()
    try:
        smtp_port = int((_getenv("SMTP_PORT", "587") or "587").strip())
    except ValueError:
        smtp_port = 587

    smtp_username = (_getenv("SMTP_USERNAME") or "").strip() or None
    smtp_password = (_getenv("SMTP_PASSWORD") or "").strip() or None

    smtp_use_tls = _parse_bool(_getenv("SMTP_USE_TLS"), default=False)
    smtp_use_starttls = _parse_bool(_getenv("SMTP_USE_STARTTLS"), default=True)

    try:
        smtp_timeout_seconds = float((_getenv("SMTP_TIMEOUT_SECONDS", "10") or "10").strip())
    except ValueError:
        smtp_timeout_seconds = 10.0

    smtp_from_email = (_getenv("SMTP_FROM_EMAIL", "") or "").strip()
    smtp_from_name = (_getenv("SMTP_FROM_NAME", "") or "").strip()

    # Import (phase-24)
    import_storage_dir = (_getenv("IMPORT_STORAGE_DIR", "var/imports") or "var/imports").strip()

    try:
        import_max_upload_mb = int((_getenv("IMPORT_MAX_UPLOAD_MB", "25") or "25").strip())
    except ValueError:
        import_max_upload_mb = 25

    fio_api_base_url = (
        _getenv(
            "FIO_API_BASE_URL",
            "https://fioapi.fio.cz/v1/rest",
        )
        or ""
    ).strip().rstrip("/")

    try:
        fio_timeout_seconds = float((_getenv("FIO_TIMEOUT_SECONDS", "30") or "30").strip())
    except ValueError:
        fio_timeout_seconds = 30.0

    payment_sync_imap_host = (_getenv("PAYMENT_SYNC_IMAP_HOST", "") or "").strip()
    try:
        payment_sync_imap_port = int((_getenv("PAYMENT_SYNC_IMAP_PORT", "993") or "993").strip())
    except ValueError:
        payment_sync_imap_port = 993
    payment_sync_imap_username = (_getenv("PAYMENT_SYNC_IMAP_USERNAME") or "").strip() or None
    payment_sync_imap_password = (_getenv("PAYMENT_SYNC_IMAP_PASSWORD") or "").strip() or None
    payment_sync_imap_mailbox = (_getenv("PAYMENT_SYNC_IMAP_MAILBOX", "INBOX") or "INBOX").strip() or "INBOX"
    payment_sync_imap_use_ssl = _parse_bool(_getenv("PAYMENT_SYNC_IMAP_USE_SSL"), default=True)
    payment_sync_alert_domain = (_getenv("PAYMENT_SYNC_ALERT_DOMAIN") or "").strip().lower() or None

    # ------------------------------------------------------------------
    # Security hardening defaults (phase-29)
    # ------------------------------------------------------------------
    # Parse login rate limit configuration. These control how many login
    # attempts are allowed per IP within a sliding window before the server
    # responds with HTTP 429. Use sensible defaults when not provided or
    # invalid.
    try:
        login_rate_limit_max = int((_getenv("LOGIN_RATE_LIMIT_MAX", "10") or "10").strip())
    except ValueError:
        login_rate_limit_max = 10
    try:
        login_rate_limit_window_seconds = int(
            (_getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60") or "60").strip()
        )
    except ValueError:
        login_rate_limit_window_seconds = 60

    try:
        api_rate_limit_max = int((_getenv("API_RATE_LIMIT_MAX", "240") or "240").strip())
    except ValueError:
        api_rate_limit_max = 240
    try:
        api_rate_limit_window_seconds = int(
            (_getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60") or "60").strip()
        )
    except ValueError:
        api_rate_limit_window_seconds = 60
    try:
        api_monthly_quota_max = int((_getenv("API_MONTHLY_QUOTA_MAX", "2500") or "2500").strip())
    except ValueError:
        api_monthly_quota_max = 2500
    api_monthly_quota_max = max(1, int(api_monthly_quota_max))

    # CSRF protection is enabled by default in production. It can be disabled
    # explicitly via CSRF_ENABLED=0 for development/testing convenience.
    csrf_enabled = _parse_bool(_getenv("CSRF_ENABLED"), default=(app_env == "prod"))
    if app_env == "prod" and not csrf_enabled:
        raise RuntimeError("CSRF_ENABLED must be enabled in production")

    return Settings(
        app_env=app_env,
        debug=debug,
        secret_key=secret_key,
        signup_token_key=signup_token_key or secret_key,
        public_link_hmac_key=public_link_hmac_key or secret_key,
        data_encryption_key=data_encryption_key or secret_key,
        database_url=database_url,

        auth_required=auth_required,
        signup_enabled=signup_enabled,
        setup_token=setup_token,
        internal_job_token=internal_job_token,
        trusted_proxy_ips=trusted_proxy_ips,

        issuer_name=issuer_name,
        issuer_email=issuer_email,
        issuer_phone=issuer_phone,
        issuer_street=issuer_street,
        issuer_city=issuer_city,
        issuer_zip=issuer_zip,
        issuer_country=issuer_country,
        issuer_ico=issuer_ico,
        issuer_dic=issuer_dic,
        issuer_bank_account=issuer_bank_account,

        ares_base_url=ares_base_url,
        ares_timeout_seconds=ares_timeout_seconds,
        company_lookup_cache_ttl_days=company_lookup_cache_ttl_days,

        sk_rpo_base_url=sk_rpo_base_url,
        sk_rpo_timeout_seconds=sk_rpo_timeout_seconds,
        sk_orsr_base_url=sk_orsr_base_url,
        sk_orsr_timeout_seconds=sk_orsr_timeout_seconds,

        pdf_storage_dir=pdf_storage_dir,

        public_base_url=public_base_url,
        app_base_url=app_base_url,
        public_rate_limit_max=public_rate_limit_max,
        public_rate_limit_window_seconds=public_rate_limit_window_seconds,

        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_use_tls=bool(smtp_use_tls),
        smtp_use_starttls=bool(smtp_use_starttls),
        smtp_timeout_seconds=float(smtp_timeout_seconds),
        smtp_from_email=smtp_from_email,
        smtp_from_name=smtp_from_name,

        import_storage_dir=import_storage_dir,
        import_max_upload_mb=int(import_max_upload_mb),
        fio_api_base_url=fio_api_base_url,
        fio_timeout_seconds=float(fio_timeout_seconds),
        payment_sync_imap_host=payment_sync_imap_host,
        payment_sync_imap_port=int(payment_sync_imap_port),
        payment_sync_imap_username=payment_sync_imap_username,
        payment_sync_imap_password=payment_sync_imap_password,
        payment_sync_imap_mailbox=payment_sync_imap_mailbox,
        payment_sync_imap_use_ssl=bool(payment_sync_imap_use_ssl),
        payment_sync_alert_domain=payment_sync_alert_domain,

        # Security hardening (phase-29)
        login_rate_limit_max=int(login_rate_limit_max),
        login_rate_limit_window_seconds=int(login_rate_limit_window_seconds),
        api_rate_limit_max=int(api_rate_limit_max),
        api_rate_limit_window_seconds=int(api_rate_limit_window_seconds),
        api_monthly_quota_max=int(api_monthly_quota_max),
        csrf_enabled=bool(csrf_enabled),
    )

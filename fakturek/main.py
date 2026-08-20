from __future__ import annotations
from fakturek.time_utils import as_utc_aware, utc_now

import calendar
from decimal import Decimal
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import re
import secrets
import time
import threading
import hashlib
import json
import os
import errno
import shutil
import tempfile
import csv
import io
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from ipaddress import ip_address
from math import ceil

from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import contextvars
import logging
import traceback
from html import escape

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from fakturek.money import (
    compute_line_amounts_cents,
    format_cents,
    format_quantity,
    parse_money_to_cents,
    parse_money_to_signed_cents,
    parse_quantity,
    parse_vat_rate,
)
from fakturek.banking import (
    BankAccountPayload,
    PaymentQRCode,
    build_payment_qr_codes,
    compute_rounding_adjustment_cents,
    digits_only,
    format_iban_for_display,
    normalize_spaces,
    resolve_bank_account,
    variable_symbol_from_invoice_number,
)
from fakturek.bank_sync import (
    BankSyncError,
    EMAIL_BANK_PARSER_OPTIONS,
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
from fakturek.settings import get_settings
from fakturek.invoice_themes import INVOICE_PDF_THEME_OPTIONS, INVOICE_PDF_THEME_DESCRIPTIONS, normalize_invoice_pdf_theme, pdf_theme_to_invoice_style
from fakturek.ui_i18n import (
    UI_LANGUAGE_OPTIONS,
    normalize_ui_language,
    translate_html_document,
    translate_ui_text,
    ui_translation_payload,
)

from fakturek.auth import hash_password, needs_rehash, new_password_length_error, verify_password
from fakturek.api_tokens import create_api_token as create_personal_api_token
from fakturek.rate_limit import SlidingWindowRateLimiter
from fakturek.security import csv_safe_cell, decrypt_secret, encrypt_secret

from fakturek.pdf import (
    InvoicePDFData,
    content_disposition_inline,
    content_disposition_attachment,
    render_error_pdf_bytes,
    render_html_pdf_bytes,
    render_invoice_pdf_bytes,
)

from fakturek.pdf_store import (
    persist_pdf_bytes,
    read_pdf_bytes,
    resolve_storage_root,
    safe_filename_base,
)

from fakturek.emailing import (
    SMTPConfig,
    build_email_message,
    is_configured as smtp_is_configured,
    looks_like_email,
    send_via_smtp,
    split_recipients,
)
from fakturek.export_formats import (
    build_money_s3_invoice_export_bytes,
    build_pohoda_invoice_export_bytes,
)
from fakturek.isdoc import build_isdoc_bytes
from fakturek.public_links import (
    PUBLIC_USERNAME_RE,
    build_public_invoice_urls,
    parse_public_invoice_short_code,
    resolve_public_base_url,
    ensure_invoice_public_link,
    ensure_subject_public_username,
    generate_unique_invoice_public_token,
    slugify_public_invoice_number,
    verify_public_invoice_short_code,
)
from fakturek.extensions import register_optional_extensions


def create_app() -> FastAPI:
    settings = get_settings()

    # The browser application has no public machine API of its own. Keeping
    # FastAPI's generated root schema enabled would expose an inventory of all
    # internal HTML and job routes. The intentionally public API v1 mounts its
    # separately documented schema below /api/v1.
    app = FastAPI(
        title="fakturek",
        debug=settings.debug,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    project_root = Path(__file__).resolve().parents[1]
    templates = Jinja2Templates(directory=str(project_root / "templates"))
    templates.env.globals["safe_bank_sync_error_message"] = safe_bank_sync_error_message

    # ------------------------------------------------------------------
    # Template globals for environment-driven build metadata and optional
    # bootstrap credential display during local/dev onboarding.
    # ------------------------------------------------------------------
    # These are intentionally environment-driven so deployments can
    # display a build/version string and (optionally) bootstrap credentials
    # on the login page.

    def _env_bool(name: str, default: bool = False) -> bool:
        v = os.getenv(name)
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {'1', 'true', 'yes', 'y', 'on'}:
            return True
        if s in {'0', 'false', 'no', 'n', 'off'}:
            return False
        return default

    # ------------------------------------------------------------------
    # Logging + verbose error pages for debugging self-hosted deployments.
    # ------------------------------------------------------------------
    # FastAPI's default in non-debug mode is a generic
    # "Internal Server Error", so we provide an opt-in verbose page.
    #
    # We keep FastAPI debug=False by default, but we:
    # - always log full tracebacks to a dedicated file when possible
    # - optionally render a verbose HTML error page with the traceback
    #
    # Toggle via env:
    #   FAKTUREK_VERBOSE_ERRORS=1
    #   FAKTUREK_LOG_DIR=/state (or /workspace/var)

    verbose_errors = _env_bool("FAKTUREK_VERBOSE_ERRORS", default=settings.debug)
    log_dir_env = (os.getenv("FAKTUREK_LOG_DIR") or "").strip()

    # Best-effort: attach a file handler to the root logger so *all* logs
    # (including uvicorn/fastapi/sqlalchemy) get written somewhere stable.
    def _ensure_file_logging() -> str | None:
        if not log_dir_env:
            return None
        try:
            log_dir = Path(log_dir_env)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "errors.log"
            root_logger = logging.getLogger()
            for h in list(root_logger.handlers):
                try:
                    if isinstance(h, logging.FileHandler) and Path(getattr(h, "baseFilename", "")) == log_path:
                        return str(log_path)
                except Exception:
                    continue
            fh = logging.FileHandler(str(log_path), encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            root_logger.addHandler(fh)
            # Do not force root level lower if uvicorn configured it.
            if root_logger.level in (logging.NOTSET, 0):
                root_logger.setLevel(logging.INFO)
            return str(log_path)
        except Exception:
            return None

    errors_log_path = _ensure_file_logging()

    class _VerboseErrorMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            req_id = uuid4().hex
            # Store for templates / other code.
            try:
                request.state.request_id = req_id  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                resp = await call_next(request)
                try:
                    resp.headers["X-Request-ID"] = req_id
                except Exception:
                    pass
                return resp
            except Exception as exc:
                # Log full traceback.
                logging.getLogger("fakturek").exception(
                    "Unhandled error request_id=%s method=%s path=%s", req_id, request.method, request.url.path
                )

                wants_html = "text/html" in (request.headers.get("accept") or "")
                if not wants_html:
                    payload: dict[str, object] = {"detail": "Internal Server Error", "request_id": req_id}
                    if verbose_errors:
                        payload["error"] = f"{type(exc).__name__}: {str(exc)}"
                        payload["traceback"] = traceback.format_exc()
                        if errors_log_path:
                            payload["errors_log"] = errors_log_path
                    resp = JSONResponse(status_code=500, content=payload)
                    resp.headers["X-Request-ID"] = req_id
                    return _apply_security_headers(request, resp)

                # HTML responses stay generic unless verbose errors were
                # explicitly enabled. Tracebacks and local log paths may
                # contain credentials, SQL, or deployment details.
                msg = "Internal Server Error"
                extra = ""
                log_hint = ""
                if verbose_errors:
                    msg = f"{type(exc).__name__}: {str(exc)}".strip() or msg
                    tb = traceback.format_exc()
                    extra = f"<h2>Traceback</h2><pre>{escape(tb)}</pre>"
                    if errors_log_path:
                        log_hint = (
                            '<p class="muted"><small>Log: '
                            f"<code>{escape(errors_log_path)}</code></small></p>"
                        )

                html = (
                    "<!doctype html><html lang=\"cs\"><head><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                    "<title>Internal Server Error</title>"
                    "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial;"
                    "background:#121212;color:#fff;margin:0;padding:24px}"
                    "a{color:#81c784}code,pre{background:#1e1e1e;color:#e0e0e0;padding:8px;border-radius:8px}"
                    "pre{overflow:auto}h1,h2{margin:0 0 12px}p{margin:8px 0}.muted{color:#b0b0b0}"
                    "</style></head><body>"
                    f"<h1>Internal Server Error</h1><p><strong>{escape(msg)}</strong></p>"
                    f"<p class=\"muted\">request_id: <code>{escape(req_id)}</code></p>"
                    f"{log_hint}"
                    f"{extra}"
                    "</body></html>"
                )
                resp = HTMLResponse(content=html, status_code=500)
                resp.headers["X-Request-ID"] = req_id
                return _apply_security_headers(request, resp)

    def _load_bootstrap_credentials() -> dict[str, str] | None:
        if settings.app_env == "prod":
            return None
        show_bootstrap = _env_bool("FAKTUREK_SHOW_BOOTSTRAP_CREDS", default=False)
        if not show_bootstrap:
            return None
        username = (os.getenv("FAKTUREK_BOOTSTRAP_USERNAME") or "").strip()
        if not username:
            return None
        password = (os.getenv("FAKTUREK_BOOTSTRAP_PASSWORD") or "").strip()
        # If password isn't present in env, we still show the username only.
        return {'username': username, 'password': password}

    # Phase-62: bump build number. When APP_BUILD env is set it takes priority.
    templates.env.globals['app_build'] = (os.getenv('APP_BUILD') or '62').strip() or '62'
    templates.env.globals['app_base_url'] = (str(getattr(settings, 'app_base_url', '') or '').rstrip('/') if str(getattr(settings, 'app_base_url', '') or '').strip() not in {'', '/'} else '')
    templates.env.globals['bootstrap_credentials'] = _load_bootstrap_credentials()

    _public_rate_limiter = SlidingWindowRateLimiter(
        max_requests=int(getattr(settings, "public_rate_limit_max", 120) or 120),
        window_seconds=int(getattr(settings, "public_rate_limit_window_seconds", 60) or 60),
    )

    # ------------------------------------------------------------------
    # Login rate limiter (phase-29)
    # ------------------------------------------------------------------
    # Similar sliding window limiter to deter brute-force login attacks. The limits
    # can be tuned via LOGIN_RATE_LIMIT_MAX and LOGIN_RATE_LIMIT_WINDOW_SECONDS.
    _login_rate_limiter = SlidingWindowRateLimiter(
        max_requests=int(getattr(settings, "login_rate_limit_max", 10) or 10),
        window_seconds=int(getattr(settings, "login_rate_limit_window_seconds", 60) or 60),
    )
    _auth_email_rate_limiter = SlidingWindowRateLimiter(
        max_requests=max(1, min(5, int(getattr(settings, "login_rate_limit_max", 10) or 10))),
        window_seconds=max(60, int(getattr(settings, "login_rate_limit_window_seconds", 60) or 60)),
    )

    SESSION_MAX_AGE_OPTIONS = [
        (1, "1 den"),
        (3, "3 dny"),
        (7, "1 týden"),
        (14, "2 týdny"),
        (30, "30 dní"),
    ]

    def _normalize_session_max_age_days(value: object | None) -> int:
        try:
            days = int(str(value or "").strip())
        except Exception:
            days = 7
        allowed = {int(v) for v, _label in SESSION_MAX_AGE_OPTIONS}
        return days if days in allowed else 7

    def _login_rate_limit_or_429(request: Request) -> None:
        # Rate limit based on client IP. In production a proper middleware could
        # consider additional headers (e.g. X-Forwarded-For) as needed.
        ip = _client_ip(request)
        key = f"login:{ip}"
        decision = _login_rate_limiter.check(key)
        if decision.allowed:
            return
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts; please try again later.",
            headers={"Retry-After": str(decision.retry_after)},
        )

    def _auth_email_rate_limit_or_429(request: Request) -> None:
        decision = _auth_email_rate_limiter.check(f"auth-email:{_client_ip(request)}")
        if decision.allowed:
            return
        raise HTTPException(
            status_code=429,
            detail="Too many email requests; please try again later.",
            headers={"Retry-After": str(decision.retry_after)},
        )


    # ------------------------------------------------------------------
    # CSRF token helpers (phase-29)
    # ------------------------------------------------------------------
    def _ensure_csrf_token(request: Request) -> str:
        """Ensure a CSRF token is present in the session and return it."""
        try:
            token = request.session.get("csrf_token")  # type: ignore[assignment]
        except Exception:
            token = None
        if not token:
            token = secrets.token_hex(32)
            try:
                request.session["csrf_token"] = token
            except Exception:
                # If session is unavailable, fall back to random per-request token.
                pass
        return str(token)

    def _csrf_exempt_path(path: str) -> bool:
        current = str(path or "").strip() or "/"
        if current.startswith("/internal/jobs/"):
            return True
        if current.startswith("/api/v1"):
            return True
        try:
            return _is_public_invoice_path(current)
        except Exception:
            return False

    def _path_requires_export_permission(path: str) -> bool:
        """Return True for internal endpoints that download/export subject data.

        Keep this server-side list in sync with export/download routes. UI hiding is
        only a convenience; RBAC must be enforced here because these endpoints are
        easy to call directly.
        """
        current = str(path or "").strip() or "/"
        if current in {
            "/contacts/export.csv",
            "/invoices/export.csv",
            "/exports/data.zip",
            "/exports/invoices",
        }:
            return True
        return re.fullmatch(r"/invoices/\d+/(pdf|isdoc|cash-receipt/pdf)", current) is not None


    async def _request_form_once(request: Request):
        """Parse request form data at most once per request.

        This is important for multipart uploads: CSRF middleware may need to
        inspect the form token, but the endpoint still has to access uploaded
        files afterwards. We cache the parsed form on the ASGI scope so both
        middleware and endpoint reuse the same object.
        """
        cache_key = "fakturek.cached_form"
        if cache_key in request.scope:
            return request.scope[cache_key]
        form = await request.form()
        request.scope[cache_key] = form
        return form

    async def _verify_csrf(request: Request) -> None:
        """
        Verify the CSRF token on mutating requests. Raises HTTPException if
        verification fails. When CSRF is disabled (settings.csrf_enabled=False)
        or the request method is safe (GET/HEAD/OPTIONS/TRACE), the check is
        skipped. The token is accepted via the standard form field "csrf_token"
        or the header "X-CSRF-Token".
        """
        if not bool(getattr(settings, "csrf_enabled", False)):
            return
        # Only enforce on state-changing methods.
        if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if _csrf_exempt_path(_request_scope_path(request)):
            return
        try:
            session_token = request.session.get("csrf_token")  # type: ignore[assignment]
        except Exception:
            session_token = None
        if not session_token:
            raise HTTPException(status_code=403, detail="CSRF token missing in session")
        # Prefer header for API use cases.
        form_token: str | None = request.headers.get("x-csrf-token")
        if not form_token:
            content_type = str(request.headers.get("content-type") or "").lower()
            if content_type.startswith("application/x-www-form-urlencoded"):
                try:
                    body_bytes = await request.body()
                    parsed = parse_qs(body_bytes.decode("utf-8", "ignore"), keep_blank_values=True)
                    values = parsed.get("csrf_token") or []
                    if values:
                        form_token = str(values[0] or "")
                except Exception:
                    form_token = None
        if not form_token:
            # Attempt to read from form data. This can raise if request body is invalid.
            try:
                form = await _request_form_once(request)
                form_token = str(form.get("csrf_token") or "")
            except Exception:
                form_token = None
        if not form_token or not secrets.compare_digest(str(form_token), str(session_token)):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _detailed_errors_enabled() -> bool:
        return bool(verbose_errors or settings.debug or settings.app_env != "prod")

    def _safe_exception_message(exc: Exception | str | None, *, fallback: str) -> str:
        if _detailed_errors_enabled():
            detail = str(exc or "").strip()
            if detail:
                return detail
        return str(fallback)

    def _safe_db_error_message(exc: Exception | str | None = None) -> str:
        source = exc if exc is not None else _db_import_error
        return _safe_exception_message(source, fallback="Databáze je dočasně nedostupná.")

    def _safe_operation_error(exc: Exception | str | None, *, fallback: str) -> str:
        """Keep operational details in development without leaking them in production."""

        if _detailed_errors_enabled():
            detail = str(exc or "").strip()
            if detail:
                return f"{fallback}: {detail}"
        return str(fallback)

    def _client_ip(request: Request) -> str:
        direct_ip = ""
        try:
            direct_ip = str(request.client.host if request.client else "").strip()
        except Exception:
            direct_ip = ""

        def _validated_ip(value: object | None) -> str:
            try:
                return str(ip_address(str(value or "").strip()))
            except ValueError:
                return ""

        trusted_proxies = {
            str(item or "").strip().lower()
            for item in (getattr(settings, "trusted_proxy_ips", ()) or ())
            if str(item or "").strip()
        }
        if direct_ip.lower() in trusted_proxies:
            xff = str(request.headers.get("x-forwarded-for") or "").strip()
            if xff:
                chain = [part.strip() for part in xff.split(",") if part.strip()]
                for candidate in reversed(chain):
                    validated = _validated_ip(candidate)
                    if validated and validated.lower() not in trusted_proxies:
                        return validated
        return _validated_ip(direct_ip) or direct_ip[:45] or "unknown"

    def _request_scope_path(request: Request) -> str:
        """Return the canonical ASGI path for auth / ACL decisions.

        We intentionally avoid ``request.url.path`` here. Older Starlette
        versions can derive confusing values from malformed absolute-form
        requests, while ``scope['path']`` is the routing path parsed by the
        ASGI server itself.
        """

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


    def _request_scope_query(request: Request) -> str:
        """Return the canonical ASGI query string for redirects/UI helpers."""

        try:
            raw_query = request.scope.get("query_string")
        except Exception:
            raw_query = None
        if isinstance(raw_query, (bytes, bytearray)):
            try:
                return raw_query.decode("utf-8", "ignore")
            except Exception:
                return ""
        current = str(raw_query or "").strip()
        if current:
            return current
        try:
            fallback = str(request.url.query or "").strip()
        except Exception:
            fallback = ""
        return fallback

    _api_token_created_flash_lock = threading.Lock()
    _api_token_created_flash_store: dict[str, tuple[float, int, dict[str, str]]] = {}
    _api_token_created_flash_ttl_seconds = 5 * 60

    def _prune_api_token_created_flash(now: float | None = None) -> None:
        ts = time.time() if now is None else float(now)
        expired = [
            key
            for key, (expires_at, _user_id, _payload) in _api_token_created_flash_store.items()
            if float(expires_at) <= ts
        ]
        for key in expired:
            _api_token_created_flash_store.pop(key, None)

    def _store_api_token_created_flash(request: Request, *, user_id: int, payload: dict[str, str]) -> None:
        """Store the one-time plaintext API token server-side only.

        Starlette sessions are signed client-side cookies, not encrypted storage.
        The cookie may carry the opaque reference, but never the plaintext token.
        """
        ref = secrets.token_urlsafe(32)
        safe_payload = {str(key): str(value or "") for key, value in dict(payload or {}).items()}
        with _api_token_created_flash_lock:
            now = time.time()
            _prune_api_token_created_flash(now)
            _api_token_created_flash_store[ref] = (
                now + _api_token_created_flash_ttl_seconds,
                int(user_id),
                safe_payload,
            )
        request.session["api_token_created_ref"] = ref

    def _pop_api_token_created_flash(request: Request, *, user_id: int | None) -> dict[str, str]:
        try:
            ref = str(request.session.pop("api_token_created_ref", "") or "").strip()
        except Exception:
            ref = ""
        # Backward-compatible cleanup: old builds stored plaintext here. Drop it
        # without rendering so stale cookies do not leak tokens after deploy.
        try:
            request.session.pop("api_token_created", None)
        except Exception:
            pass
        if not ref or user_id is None:
            return {}
        with _api_token_created_flash_lock:
            _prune_api_token_created_flash()
            row = _api_token_created_flash_store.pop(ref, None)
        if row is None:
            return {}
        expires_at, stored_user_id, payload = row
        if float(expires_at) <= time.time() or int(stored_user_id) != int(user_id):
            return {}
        return dict(payload)


    def _is_public_invoice_path(path: str) -> bool:
        # Supported public patterns:
        #   /<public_username>/i/<token>/<invoice_number>[...]
        #   /i/<token> (optional helper)
        p = (path or "/").strip("/")
        if not p:
            return False
        parts = [seg for seg in p.split("/") if seg]
        if not parts:
            return False
        if parts[0] == "i" and len(parts) >= 2 and parts[1]:
            suffix = parts[2:]
            if not suffix:
                return True
            if len(suffix) == 1:
                return bool(suffix[0])
            if len(suffix) == 2:
                return bool(suffix[0]) and suffix[1] in {"pdf", "isdoc"}
            return False
        if len(parts) >= 4 and parts[1] == "i" and parts[0] and parts[2] and parts[3]:
            suffix = parts[4:]
            if not suffix:
                return True
            if len(suffix) == 1:
                return suffix[0] in {"pdf", "isdoc"}
            return False
        return False

    def _public_rate_limit_or_429(request: Request, *, key_suffix: str = "") -> None:
        ip = _client_ip(request)
        bucket = str(key_suffix or "").strip().lower()
        if not bucket:
            bucket = "pdf" if _request_scope_path(request).rstrip("/").endswith("/pdf") else "view"
        key = f"public:{ip}:{bucket}"
        decision = _public_rate_limiter.check(key)
        if decision.allowed:
            return
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )

    # ------------------------------------------------------------------
    # Persisted PDF storage root (phase-20)
    # ------------------------------------------------------------------
    static_dir = project_root / "static"
    pdf_storage_root = resolve_storage_root(settings.pdf_storage_dir, project_root=project_root)
    try:
        # Keep storage outside the mounted /static webroot by default.
        if pdf_storage_root.is_relative_to(static_dir.resolve()):
            pdf_storage_root = (project_root / "var" / "pdfs").resolve()
    except Exception:
        # Path.is_relative_to can fail on some edge cases; keep best-effort.
        pass

    # ------------------------------------------------------------------
    # Import storage root (phase-24)
    # ------------------------------------------------------------------
    import_storage_root = resolve_storage_root(settings.import_storage_dir, project_root=project_root)
    try:
        # Keep storage outside the mounted /static webroot by default.
        if import_storage_root.is_relative_to(static_dir.resolve()):
            import_storage_root = (project_root / "var" / "imports").resolve()
    except Exception:
        pass

    templates.env.filters["money"] = format_cents
    templates.env.filters["quantity"] = format_quantity
    templates.env.filters["iban_display"] = format_iban_for_display

    def _invoice_status_label(status: str | None) -> str:
        return _invoice_status_label_for_lang(status, "cs")

    templates.env.filters["invoice_status"] = _invoice_status_label

    INVOICE_PAYMENT_METHOD_OPTIONS: list[tuple[str, str]] = [
        ("bank_transfer", "Převodem"),
        ("cash", "Hotově"),
        ("card", "Kartou"),
        ("cod", "Dobírkou"),
    ]
    INVOICE_LANGUAGE_OPTIONS: list[tuple[str, str]] = [
        ("cs", "Čeština"),
        ("en", "English"),
    ]
    INVOICE_STYLE_OPTIONS: list[tuple[str, str]] = [
        ("modern", "Standard"),
        ("classic", "Klasický"),
        ("minimal", "Minimal"),
    ]
    INVOICE_DOCUMENT_TYPE_OPTIONS: list[tuple[str, str]] = [
        ("quote", "Nabídka"),
        ("invoice", "Faktura"),
        ("credit_note", "Dobropis"),
        ("proforma", "Zálohová faktura"),
    ]
    SUBJECT_ROLE_OPTIONS: list[tuple[str, str]] = [
        ("owner", "Owner"),
        ("manager", "Manager"),
        ("accountant", "Účetní"),
        ("viewer", "Pouze čtení"),
    ]
    RECURRING_INTERVAL_OPTIONS: list[tuple[str, str]] = [
        ("week", "Týdně"),
        ("month", "Měsíčně"),
    ]
    RECURRING_TEMPLATE_MARKER = "[[recurring-template]]"
    TAX_REGIME_OPTIONS: list[tuple[str, str]] = [
        ("standard", "Klasické přiznání a přehledy"),
        ("flat", "Paušální daň"),
    ]
    SUBJECT_LEGAL_FORM_OPTIONS: list[tuple[str, str]] = [
        ("business", "Podnikatel / OSVČ"),
        ("company", "Firma"),
        ("association", "Spolek / nezisková organizace"),
        ("other", "Jiný subjekt"),
    ]
    FLAT_TAX_BAND_OPTIONS: list[tuple[str, str]] = [
        ("1", "I. pásmo"),
        ("2", "II. pásmo"),
        ("3", "III. pásmo"),
    ]
    FLAT_TAX_INCOME_PROFILE_OPTIONS: list[tuple[str, str]] = [
        ("general", "Bez převahy činností s 80% / 60% výdaji"),
        ("mostly_80_60", "Alespoň 75 % příjmů z činností s 80% nebo 60% výdaji"),
        ("mostly_80", "Alespoň 75 % příjmů z činností s 80% výdaji"),
    ]
    UI_THEME_OPTIONS: list[tuple[str, str]] = [
        ("system", "Podle systému"),
        ("auto", "Podle denní doby"),
        ("light", "Světlý motiv"),
        ("dark", "Tmavý motiv"),
    ]
    CONTACT_COUNTRY_PRIORITY_CODES: tuple[str, ...] = ("CZ", "SK", "DE", "AT", "PL")
    CONTACT_COUNTRY_OPTIONS_ALL: list[tuple[str, str]] = [
        ("AD", "AD – Andorra"),
        ("AE", "AE – Spojené arabské emiráty"),
        ("AF", "AF – Afghánistán"),
        ("AG", "AG – Antigua a Barbuda"),
        ("AI", "AI – Anguilla"),
        ("AL", "AL – Albánie"),
        ("AM", "AM – Arménie"),
        ("AO", "AO – Angola"),
        ("AQ", "AQ – Antarktida"),
        ("AR", "AR – Argentina"),
        ("AS", "AS – Americká Samoa"),
        ("AT", "AT – Rakousko"),
        ("AU", "AU – Austrálie"),
        ("AW", "AW – Aruba"),
        ("AX", "AX – Alandy"),
        ("AZ", "AZ – Ázerbájdžán"),
        ("BA", "BA – Bosna a Hercegovina"),
        ("BB", "BB – Barbados"),
        ("BD", "BD – Bangladéš"),
        ("BE", "BE – Belgie"),
        ("BF", "BF – Burkina Faso"),
        ("BG", "BG – Bulharsko"),
        ("BH", "BH – Bahrajn"),
        ("BI", "BI – Burundi"),
        ("BJ", "BJ – Benin"),
        ("BL", "BL – Svatý Bartoloměj"),
        ("BM", "BM – Bermudy"),
        ("BN", "BN – Brunej"),
        ("BO", "BO – Bolívie"),
        ("BQ", "BQ – Bonaire, Saba a Sint Eustatius"),
        ("BR", "BR – Brazílie"),
        ("BS", "BS – Bahamy"),
        ("BT", "BT – Bhútán"),
        ("BV", "BV – Bouvetův ostrov"),
        ("BW", "BW – Botswana"),
        ("BY", "BY – Bělorusko"),
        ("BZ", "BZ – Belize"),
        ("CA", "CA – Kanada"),
        ("CC", "CC – Kokosové ostrovy"),
        ("CD", "CD – Demokratická republika Kongo"),
        ("CF", "CF – Středoafrická republika"),
        ("CG", "CG – Kongo"),
        ("CH", "CH – Švýcarsko"),
        ("CI", "CI – Pobřeží slonoviny"),
        ("CK", "CK – Cookovy ostrovy"),
        ("CL", "CL – Chile"),
        ("CM", "CM – Kamerun"),
        ("CN", "CN – Čína"),
        ("CO", "CO – Kolumbie"),
        ("CR", "CR – Kostarika"),
        ("CU", "CU – Kuba"),
        ("CV", "CV – Kapverdy"),
        ("CW", "CW – Curaçao"),
        ("CX", "CX – Vánoční ostrov"),
        ("CY", "CY – Kypr"),
        ("CZ", "CZ – Česká republika"),
        ("DE", "DE – Německo"),
        ("DJ", "DJ – Džibutsko"),
        ("DK", "DK – Dánsko"),
        ("DM", "DM – Dominika"),
        ("DO", "DO – Dominikánská republika"),
        ("DZ", "DZ – Alžírsko"),
        ("EC", "EC – Ekvádor"),
        ("EE", "EE – Estonsko"),
        ("EG", "EG – Egypt"),
        ("EH", "EH – Západní Sahara"),
        ("ER", "ER – Eritrea"),
        ("ES", "ES – Španělsko"),
        ("ET", "ET – Etiopie"),
        ("FI", "FI – Finsko"),
        ("FJ", "FJ – Fidži"),
        ("FK", "FK – Falklandy"),
        ("FM", "FM – Mikronésie"),
        ("FO", "FO – Faerské ostrovy"),
        ("FR", "FR – Francie"),
        ("GA", "GA – Gabon"),
        ("GB", "GB – Spojené království"),
        ("GD", "GD – Grenada"),
        ("GE", "GE – Gruzie"),
        ("GF", "GF – Francouzská Guyana"),
        ("GG", "GG – Guernsey"),
        ("GH", "GH – Ghana"),
        ("GI", "GI – Gibraltar"),
        ("GL", "GL – Grónsko"),
        ("GM", "GM – Gambie"),
        ("GN", "GN – Guinea"),
        ("GP", "GP – Guadeloupe"),
        ("GQ", "GQ – Rovníková Guinea"),
        ("GR", "GR – Řecko"),
        ("GS", "GS – Jižní Georgie a Jižní Sandwichovy ostrovy"),
        ("GT", "GT – Guatemala"),
        ("GU", "GU – Guam"),
        ("GW", "GW – Guinea-Bissau"),
        ("GY", "GY – Guyana"),
        ("HK", "HK – Hongkong"),
        ("HM", "HM – Heardův ostrov a McDonaldovy ostrovy"),
        ("HN", "HN – Honduras"),
        ("HR", "HR – Chorvatsko"),
        ("HT", "HT – Haiti"),
        ("HU", "HU – Maďarsko"),
        ("ID", "ID – Indonésie"),
        ("IE", "IE – Irsko"),
        ("IL", "IL – Izrael"),
        ("IM", "IM – Ostrov Man"),
        ("IN", "IN – Indie"),
        ("IO", "IO – Britské indickooceánské území"),
        ("IQ", "IQ – Irák"),
        ("IR", "IR – Írán"),
        ("IS", "IS – Island"),
        ("IT", "IT – Itálie"),
        ("JE", "JE – Jersey"),
        ("JM", "JM – Jamajka"),
        ("JO", "JO – Jordánsko"),
        ("JP", "JP – Japonsko"),
        ("KE", "KE – Keňa"),
        ("KG", "KG – Kyrgyzstán"),
        ("KH", "KH – Kambodža"),
        ("KI", "KI – Kiribati"),
        ("KM", "KM – Komory"),
        ("KN", "KN – Svatý Kryštof a Nevis"),
        ("KP", "KP – Severní Korea"),
        ("KR", "KR – Jižní Korea"),
        ("KW", "KW – Kuvajt"),
        ("KY", "KY – Kajmanské ostrovy"),
        ("KZ", "KZ – Kazachstán"),
        ("LA", "LA – Laos"),
        ("LB", "LB – Libanon"),
        ("LC", "LC – Svatá Lucie"),
        ("LI", "LI – Lichtenštejnsko"),
        ("LK", "LK – Srí Lanka"),
        ("LR", "LR – Libérie"),
        ("LS", "LS – Lesotho"),
        ("LT", "LT – Litva"),
        ("LU", "LU – Lucembursko"),
        ("LV", "LV – Lotyšsko"),
        ("LY", "LY – Libye"),
        ("MA", "MA – Maroko"),
        ("MC", "MC – Monako"),
        ("MD", "MD – Moldavsko"),
        ("ME", "ME – Černá Hora"),
        ("MF", "MF – Svatý Martin (Francie)"),
        ("MG", "MG – Madagaskar"),
        ("MH", "MH – Marshallovy ostrovy"),
        ("MK", "MK – Severní Makedonie"),
        ("ML", "ML – Mali"),
        ("MM", "MM – Myanmar"),
        ("MN", "MN – Mongolsko"),
        ("MO", "MO – Macao"),
        ("MP", "MP – Severní Mariany"),
        ("MQ", "MQ – Martinik"),
        ("MR", "MR – Mauritánie"),
        ("MS", "MS – Montserrat"),
        ("MT", "MT – Malta"),
        ("MU", "MU – Mauricius"),
        ("MV", "MV – Maledivy"),
        ("MW", "MW – Malawi"),
        ("MX", "MX – Mexiko"),
        ("MY", "MY – Malajsie"),
        ("MZ", "MZ – Mosambik"),
        ("NA", "NA – Namibie"),
        ("NC", "NC – Nová Kaledonie"),
        ("NE", "NE – Niger"),
        ("NF", "NF – Norfolk"),
        ("NG", "NG – Nigérie"),
        ("NI", "NI – Nikaragua"),
        ("NL", "NL – Nizozemsko"),
        ("NO", "NO – Norsko"),
        ("NP", "NP – Nepál"),
        ("NR", "NR – Nauru"),
        ("NU", "NU – Niue"),
        ("NZ", "NZ – Nový Zéland"),
        ("OM", "OM – Omán"),
        ("PA", "PA – Panama"),
        ("PE", "PE – Peru"),
        ("PF", "PF – Francouzská Polynésie"),
        ("PG", "PG – Papua-Nová Guinea"),
        ("PH", "PH – Filipíny"),
        ("PK", "PK – Pákistán"),
        ("PL", "PL – Polsko"),
        ("PM", "PM – Saint-Pierre a Miquelon"),
        ("PN", "PN – Pitcairnovy ostrovy"),
        ("PR", "PR – Portoriko"),
        ("PS", "PS – Palestina"),
        ("PT", "PT – Portugalsko"),
        ("PW", "PW – Palau"),
        ("PY", "PY – Paraguay"),
        ("QA", "QA – Katar"),
        ("RE", "RE – Réunion"),
        ("RO", "RO – Rumunsko"),
        ("RS", "RS – Srbsko"),
        ("RU", "RU – Rusko"),
        ("RW", "RW – Rwanda"),
        ("SA", "SA – Saúdská Arábie"),
        ("SB", "SB – Šalomounovy ostrovy"),
        ("SC", "SC – Seychely"),
        ("SD", "SD – Súdán"),
        ("SE", "SE – Švédsko"),
        ("SG", "SG – Singapur"),
        ("SH", "SH – Svatá Helena"),
        ("SI", "SI – Slovinsko"),
        ("SJ", "SJ – Špicberky a Jan Mayen"),
        ("SK", "SK – Slovensko"),
        ("SL", "SL – Sierra Leone"),
        ("SM", "SM – San Marino"),
        ("SN", "SN – Senegal"),
        ("SO", "SO – Somálsko"),
        ("SR", "SR – Surinam"),
        ("SS", "SS – Jižní Súdán"),
        ("ST", "ST – Svatý Tomáš a Princův ostrov"),
        ("SV", "SV – Salvador"),
        ("SX", "SX – Sint Maarten"),
        ("SY", "SY – Sýrie"),
        ("SZ", "SZ – Eswatini"),
        ("TC", "TC – Turks a Caicos"),
        ("TD", "TD – Čad"),
        ("TF", "TF – Francouzská jižní území"),
        ("TG", "TG – Togo"),
        ("TH", "TH – Thajsko"),
        ("TJ", "TJ – Tádžikistán"),
        ("TK", "TK – Tokelau"),
        ("TL", "TL – Východní Timor"),
        ("TM", "TM – Turkmenistán"),
        ("TN", "TN – Tunisko"),
        ("TO", "TO – Tonga"),
        ("TR", "TR – Turecko"),
        ("TT", "TT – Trinidad a Tobago"),
        ("TV", "TV – Tuvalu"),
        ("TW", "TW – Tchaj-wan"),
        ("TZ", "TZ – Tanzanie"),
        ("UA", "UA – Ukrajina"),
        ("UG", "UG – Uganda"),
        ("UM", "UM – Menší odlehlé ostrovy USA"),
        ("US", "US – Spojené státy americké"),
        ("UY", "UY – Uruguay"),
        ("UZ", "UZ – Uzbekistán"),
        ("VA", "VA – Vatikán"),
        ("VC", "VC – Svatý Vincenc a Grenadiny"),
        ("VE", "VE – Venezuela"),
        ("VG", "VG – Britské Panenské ostrovy"),
        ("VI", "VI – Americké Panenské ostrovy"),
        ("VN", "VN – Vietnam"),
        ("VU", "VU – Vanuatu"),
        ("WF", "WF – Wallis a Futuna"),
        ("WS", "WS – Samoa"),
        ("YE", "YE – Jemen"),
        ("YT", "YT – Mayotte"),
        ("ZA", "ZA – Jihoafrická republika"),
        ("ZM", "ZM – Zambie"),
        ("ZW", "ZW – Zimbabwe"),
    ]
    CONTACT_COUNTRY_OPTION_MAP: dict[str, str] = dict(CONTACT_COUNTRY_OPTIONS_ALL)
    CONTACT_COUNTRY_OPTIONS_TOP: list[tuple[str, str]] = [
        (code, CONTACT_COUNTRY_OPTION_MAP[code]) for code in CONTACT_COUNTRY_PRIORITY_CODES
    ]
    CONTACT_COUNTRY_OPTIONS_REST: list[tuple[str, str]] = [
        option for option in CONTACT_COUNTRY_OPTIONS_ALL if option[0] not in CONTACT_COUNTRY_PRIORITY_CODES
    ]
    CONTACT_COUNTRY_CODES: set[str] = {code for code, _label in CONTACT_COUNTRY_OPTIONS_ALL}
    PAYMENT_SYNC_PROVIDER_OPTIONS: list[tuple[str, str]] = [
        ("none", "Bez automatického párování"),
        ("fio_api", "Fio API"),
        ("email_bank", "E-mail banky"),
    ]
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

    def _normalize_tax_regime(value: str | None) -> str:
        normalized = str(value or "standard").strip().lower() or "standard"
        if normalized not in {item for item, _label in TAX_REGIME_OPTIONS}:
            return "standard"
        return normalized

    def _normalize_subject_legal_form(value: str | None) -> str:
        normalized = str(value or "business").strip().lower() or "business"
        if normalized not in {item for item, _label in SUBJECT_LEGAL_FORM_OPTIONS}:
            return "business"
        return normalized

    def _subject_uses_business_tax_limits(value: str | None) -> bool:
        return _normalize_subject_legal_form(value) in {"business", "company"}

    def _normalize_flat_tax_band(value: str | None) -> str:
        normalized = str(value or "1").strip() or "1"
        if normalized not in {item for item, _label in FLAT_TAX_BAND_OPTIONS}:
            return "1"
        return normalized

    def _normalize_flat_tax_income_profile(value: str | None) -> str:
        normalized = str(value or "general").strip().lower() or "general"
        if normalized not in {item for item, _label in FLAT_TAX_INCOME_PROFILE_OPTIONS}:
            return "general"
        return normalized

    def _normalize_ui_theme(value: str | None) -> str:
        normalized = str(value or "system").strip().lower() or "system"
        if normalized not in {theme for theme, _label in UI_THEME_OPTIONS}:
            return "system"
        return normalized

    def _ui_text(text: object, language: object | None = None) -> str:
        if language is None:
            try:
                req = _current_request.get()  # type: ignore[name-defined]
                language = req.session.get("ui_language") if req is not None else "cs"
            except Exception:
                language = "cs"
        return translate_ui_text(text, _normalize_ui_language(language))

    def _normalize_ui_language(value: object | None) -> str:
        return normalize_ui_language(value)

    templates.env.globals["t"] = _ui_text
    templates.env.globals["_"] = _ui_text

    def _normalize_contact_country(value: object | None, *, default: str = "CZ") -> str:
        normalized = str(value or default or "CZ").strip().upper() or "CZ"
        if normalized not in CONTACT_COUNTRY_CODES:
            return str(default or "CZ").strip().upper() or "CZ"
        return normalized

    def _contact_country_template_context(selected: object | None = None) -> dict[str, object]:
        selected_country = _normalize_contact_country(selected)
        return {
            "selected_country": selected_country,
            "country_options_top": CONTACT_COUNTRY_OPTIONS_TOP,
            "country_options_rest": CONTACT_COUNTRY_OPTIONS_REST,
        }

    def _normalize_payment_sync_provider(value: str | None) -> str:
        normalized = str(value or "none").strip().lower() or "none"
        if normalized not in {provider for provider, _label in PAYMENT_SYNC_PROVIDER_OPTIONS}:
            return "none"
        return normalized

    def _payment_sync_provider_label(value: str | None) -> str:
        normalized = _normalize_payment_sync_provider(value)
        for provider, label in PAYMENT_SYNC_PROVIDER_OPTIONS:
            if provider == normalized:
                return label
        return "Bez automatického párování"

    def _normalize_payment_sync_email_parser(value: str | None) -> str:
        normalized = str(value or "pending").strip().lower() or "pending"
        if normalized not in {parser for parser, _label in EMAIL_BANK_PARSER_OPTIONS}:
            return "pending"
        return normalized

    def _payment_sync_email_parser_label(value: str | None) -> str:
        normalized = _normalize_payment_sync_email_parser(value)
        for parser, label in EMAIL_BANK_PARSER_OPTIONS:
            if parser == normalized:
                return label
        return "Bez parseru"

    def _payment_sync_email_defaults(parser_name: str | None) -> dict[str, str]:
        parser = _normalize_payment_sync_email_parser(parser_name)
        defaults = EMAIL_BANK_PARSER_DEFAULTS.get(parser, {})
        return {
            "sender": str(defaults.get("sender") or "").strip(),
            "subject": str(defaults.get("subject") or "").strip(),
            "description": str(defaults.get("description") or "").strip(),
        }

    def _generate_payment_sync_alert_localpart(db: Session) -> str:
        alphabet = "".join(ch for ch in ("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") if ch)
        while True:
            candidate = "".join(secrets.choice(alphabet) for _ in range(10))
            existing = db.scalar(
                select(func.count(SubjectBankAccount.id)).where(
                    SubjectBankAccount.payment_sync_alert_localpart == candidate
                )
            )
            if not existing:
                return candidate

    def _payment_sync_alert_email_for_localpart(localpart: str | None) -> str:
        clean_localpart = str(localpart or "").strip()
        clean_domain = str(getattr(settings, "payment_sync_alert_domain", "") or "").strip().lower()
        if not clean_localpart or not clean_domain:
            return ""
        return f"{clean_localpart}@{clean_domain}"

    def _payment_sync_alert_email_for_account(account: SubjectBankAccount | None) -> str:
        if account is None:
            return ""
        return _payment_sync_alert_email_for_localpart(getattr(account, "payment_sync_alert_localpart", None))

    def _subject_role_label(value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        for role_value, role_label in SUBJECT_ROLE_OPTIONS:
            if normalized == role_value:
                return role_label
        if normalized == "user":
            return "Uživatel"
        return normalized or "Uživatel"

    INVOICE_FOOTER_PRESET_OPTIONS: list[tuple[str, str]] = [
        ("trade_register", "Živnostenský rejstřík"),
        ("commercial_register", "Obchodní rejstřík"),
        ("association_register", "Spolkový rejstřík"),
        ("custom", "Vlastní text"),
        ("none", "Bez patičky"),
    ]
    INVOICE_FOOTER_PRESET_TEXTS: dict[str, str] = {
        "trade_register": "Fyzická osoba zapsaná v živnostenském rejstříku.",
        "commercial_register": "Společnost zapsaná v obchodním rejstříku.",
        "association_register": "Spolek zapsaný ve spolkovém rejstříku.",
        "custom": "",
        "none": "",
    }
    INVOICE_FOOTER_PRESET_TEXTS_I18N: dict[str, dict[str, str]] = {
        "cs": dict(INVOICE_FOOTER_PRESET_TEXTS),
        "en": {
            "trade_register": "Sole trader registered in the Trade Register.",
            "commercial_register": "Company registered in the Commercial Register.",
            "association_register": "Association registered in the Association Register.",
            "custom": "",
            "none": "",
        },
    }

    INVOICE_I18N: dict[str, dict[str, str]] = {
        "cs": {
            "document": "Doklad",
            "print_suffix": "tisk",
            "back": "Zpět",
            "open_pdf": "Otevřít PDF",
            "download_pdf": "Stáhnout PDF",
            "print": "Vytisknout",
            "no_buyer": "Bez odběratele",
            "cancelled": "Stornováno",
            "cancelled_text": "Tento doklad byl stornován a neslouží k úhradě.",
            "seller": "Dodavatel",
            "buyer": "Odběratel",
            "overview": "Přehled",
            "payment": "Platba",
            "invoice_number": "Číslo dokladu",
            "issue_date": "Datum vystavení",
            "taxable_supply_date": "DUZP",
            "due_date": "Datum splatnosti",
            "status": "Stav",
            "currency": "Měna",
            "payment_method": "Způsob platby",
            "account_number": "Číslo účtu",
            "reference": "Variabilní symbol",
            "bic": "BIC / SWIFT",
            "linked_document": "Navázaný doklad",
            "line_items": "Položky dokladu",
            "description": "Popis",
            "quantity": "Množství",
            "unit_price": "Jedn. cena",
            "vat": "DPH",
            "total": "Celkem",
            "subtotal": "Mezisoučet",
            "discount": "Sleva",
            "rounding": "Zaokrouhlení",
            "note": "Poznámka",
            "footer": "Patička",
            "amount_due": "Celkem k úhradě",
            "paid_on": "Uhrazeno",
            "paid_notice": "Tento doklad je již uhrazený. Neplaťte jej prosím znovu.",
            "no_items": "Bez položek.",
            "no_bank_account": "Na faktuře není vybraný bankovní účet.",
            "email_hello": "Dobrý den,",
            "email_attached": "v příloze zasílám",
            "email_due": "Splatnost",
            "email_best_regards": "S pozdravem,",
            "reminder_subject_prefix": "Upomínka",
            "reminder_intro": "posílám upomínku k dokladu",
            "reminder_amount": "na částku",
            "reminder_overdue": "dní po splatnosti",
            "reminder_request": "Prosím o úhradu co nejdříve. Pokud jste již uhradili, považujte prosím tento e-mail za bezpředmětný.",
            "email_account_number": "Číslo účtu",
            "email_reference": "VS",
            "status_draft": "draft",
            "status_issued": "vystavená",
            "status_sent": "odeslaná",
            "status_paid": "zaplacená",
            "status_cancelled": "stornovaná",
            "payment_method_bank_transfer": "Převodem",
            "payment_method_cash": "Hotově",
            "payment_method_card": "Kartou",
            "payment_method_cod": "Dobírkou",
            "document_type_quote": "Nabídka",
            "document_type_invoice": "Faktura",
            "document_type_credit_note": "Dobropis",
            "document_type_proforma": "Zálohová faktura",
            "document_detail_quote": "Detail nabídky",
            "document_detail_invoice": "Detail faktury",
            "document_detail_credit_note": "Detail dobropisu",
            "document_detail_proforma": "Detail zálohové faktury",
        },
        "en": {
            "document": "Document",
            "print_suffix": "print",
            "back": "Back",
            "open_pdf": "Open PDF",
            "download_pdf": "Download PDF",
            "print": "Print",
            "no_buyer": "No buyer",
            "cancelled": "Cancelled",
            "cancelled_text": "This document has been cancelled and is not payable.",
            "seller": "Seller",
            "buyer": "Buyer",
            "overview": "Overview",
            "payment": "Payment",
            "invoice_number": "Document number",
            "issue_date": "Issue date",
            "taxable_supply_date": "Taxable supply date",
            "due_date": "Due date",
            "status": "Status",
            "currency": "Currency",
            "payment_method": "Payment method",
            "account_number": "Account number",
            "reference": "Reference",
            "bic": "BIC / SWIFT",
            "linked_document": "Linked document",
            "line_items": "Line items",
            "description": "Description",
            "quantity": "Quantity",
            "unit_price": "Unit price",
            "vat": "VAT",
            "total": "Total",
            "subtotal": "Subtotal",
            "discount": "Discount",
            "rounding": "Rounding",
            "note": "Note",
            "footer": "Footer",
            "amount_due": "Amount due",
            "paid_on": "Paid on",
            "paid_notice": "This document has already been paid. Please do not pay it again.",
            "no_items": "No line items.",
            "no_bank_account": "No bank account is set on this invoice.",
            "email_hello": "Hello,",
            "email_attached": "please find attached",
            "email_due": "Due date",
            "email_best_regards": "Best regards,",
            "reminder_subject_prefix": "Reminder",
            "reminder_intro": "this is a reminder for",
            "reminder_amount": "in the amount of",
            "reminder_overdue": "days overdue",
            "reminder_request": "Please arrange payment as soon as possible. If you have already paid, please ignore this email.",
            "email_account_number": "Account number",
            "email_reference": "Reference",
            "status_draft": "draft",
            "status_issued": "issued",
            "status_sent": "sent",
            "status_paid": "paid",
            "status_cancelled": "cancelled",
            "payment_method_bank_transfer": "Bank transfer",
            "payment_method_cash": "Cash",
            "payment_method_card": "Card",
            "payment_method_cod": "Cash on delivery",
            "document_type_quote": "Quote",
            "document_type_invoice": "Invoice",
            "document_type_credit_note": "Credit note",
            "document_type_proforma": "Proforma invoice",
            "document_detail_quote": "Quote detail",
            "document_detail_invoice": "Invoice detail",
            "document_detail_credit_note": "Credit note detail",
            "document_detail_proforma": "Proforma invoice detail",
        },
    }

    def _normalize_invoice_language(value: str | None) -> str:
        normalized = str(value or "cs").strip().lower() or "cs"
        if normalized not in {item for item, _label in INVOICE_LANGUAGE_OPTIONS}:
            return "cs"
        return normalized

    def _normalize_invoice_style(value: str | None) -> str:
        normalized = str(value or "modern").strip().lower() or "modern"
        if normalized not in {item for item, _label in INVOICE_STYLE_OPTIONS}:
            return "modern"
        return normalized

    def _invoice_texts(language: str | None) -> dict[str, str]:
        normalized = _normalize_invoice_language(language)
        return INVOICE_I18N.get(normalized, INVOICE_I18N["cs"])

    def _invoice_text(key: str, language: str | None = None) -> str:
        texts = _invoice_texts(language)
        return str(texts.get(key) or INVOICE_I18N["cs"].get(key) or key)

    def _invoice_payment_method_label(method: str | None, language: str | None = None) -> str:
        target = (method or "").strip().lower()
        if target == "bank_transfer":
            return _invoice_text("payment_method_bank_transfer", language)
        if target == "cash":
            return _invoice_text("payment_method_cash", language)
        if target == "card":
            return _invoice_text("payment_method_card", language)
        if target == "cod":
            return _invoice_text("payment_method_cod", language)
        return target or "—"

    def _normalize_invoice_document_type(value: str | None) -> str:
        normalized = str(value or "invoice").strip().lower() or "invoice"
        if normalized not in {item for item, _label in INVOICE_DOCUMENT_TYPE_OPTIONS}:
            return "invoice"
        return normalized

    def _invoice_document_type_label(document_type: str | None, language: str | None = None) -> str:
        target = _normalize_invoice_document_type(document_type)
        if target == "quote":
            return _invoice_text("document_type_quote", language)
        if target == "credit_note":
            return _invoice_text("document_type_credit_note", language)
        if target == "proforma":
            return _invoice_text("document_type_proforma", language)
        return _invoice_text("document_type_invoice", language)

    def _invoice_document_type_kicker(document_type: str | None, language: str | None = None) -> str:
        return _invoice_document_type_label(document_type, language)

    def _invoice_document_type_detail_label(document_type: str | None, language: str | None = None) -> str:
        target = _normalize_invoice_document_type(document_type)
        if target == "quote":
            return _invoice_text("document_detail_quote", language)
        if target == "credit_note":
            return _invoice_text("document_detail_credit_note", language)
        if target == "proforma":
            return _invoice_text("document_detail_proforma", language)
        return _invoice_text("document_detail_invoice", language)

    def _invoice_document_email_subject(document_type: str | None, number: str | None, language: str | None = None) -> str:
        return f"{_invoice_document_type_label(document_type, language)} {str(number or '').strip()}".strip()

    def _invoice_status_label_for_lang(status: str | None, language: str | None = None) -> str:
        s = (status or "").strip().lower()
        return {
            "draft": _invoice_text("status_draft", language),
            "issued": _invoice_text("status_issued", language),
            "sent": _invoice_text("status_sent", language),
            "paid": _invoice_text("status_paid", language),
            "cancelled": _invoice_text("status_cancelled", language),
        }.get(s, s or "—")

    templates.env.filters["invoice_payment_method"] = _invoice_payment_method_label
    templates.env.filters["subject_role"] = _subject_role_label

    def _normalize_recurring_interval_unit(value: str | None) -> str:
        normalized = str(value or "month").strip().lower() or "month"
        if normalized not in {option for option, _label in RECURRING_INTERVAL_OPTIONS}:
            return "month"
        return normalized

    def _build_recurring_prefill(today_value: date | None = None) -> dict[str, object]:
        today_local = today_value or date.today()
        return {
            "name": "",
            "next_issue_date": today_local.isoformat(),
            "interval_unit": "month",
            "interval_count": 1,
            "due_in_days": 14,
            "auto_issue": True,
            "auto_send": False,
            "email_override": "",
        }

    def _prefill_recurring_from_form(form) -> dict[str, object]:
        base = _build_recurring_prefill()
        try:
            interval_count = max(1, int(str(form.get("interval_count") or "1").strip() or "1"))
        except Exception:
            interval_count = 1
        try:
            due_in_days = max(0, int(str(form.get("due_in_days") or "14").strip() or "14"))
        except Exception:
            due_in_days = 14
        next_issue_raw = str(form.get("next_issue_date") or "").strip()
        try:
            next_issue_date = date.fromisoformat(next_issue_raw).isoformat() if next_issue_raw else base["next_issue_date"]
        except Exception:
            next_issue_date = base["next_issue_date"]
        return {
            "name": str(form.get("name") or "").strip(),
            "next_issue_date": next_issue_date,
            "interval_unit": _normalize_recurring_interval_unit(form.get("interval_unit")),
            "interval_count": interval_count,
            "due_in_days": due_in_days,
            "auto_issue": bool(form.get("auto_issue")),
            "auto_send": bool(form.get("auto_send")),
            "email_override": str(form.get("email_override") or "").strip(),
        }

    def _mark_internal_recurring_template_note(existing_value: str | None = None) -> str:
        cleaned = str(existing_value or "").strip()
        if cleaned.startswith(RECURRING_TEMPLATE_MARKER):
            return cleaned
        return f"{RECURRING_TEMPLATE_MARKER} {cleaned}".strip()

    def _is_internal_recurring_template_invoice(invoice: Invoice | None) -> bool:
        if invoice is None:
            return False
        return str(getattr(invoice, "internal_notes", "") or "").strip().startswith(RECURRING_TEMPLATE_MARKER)

    def _invoice_visible_in_lists_clause():
        return or_(
            Invoice.internal_notes.is_(None),
            Invoice.internal_notes == "",
            ~Invoice.internal_notes.like(f"{RECURRING_TEMPLATE_MARKER}%"),
        )

    def _add_recurrence_step(base_date: date, *, interval_unit: str, interval_count: int) -> date:
        count = max(1, int(interval_count or 1))
        unit = _normalize_recurring_interval_unit(interval_unit)
        if unit == "week":
            return base_date + timedelta(days=7 * count)
        month_index = (int(base_date.month) - 1) + count
        year = int(base_date.year) + (month_index // 12)
        month = (month_index % 12) + 1
        day = min(int(base_date.day), calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def _month_end(value: date) -> date:
        return date(int(value.year), int(value.month), calendar.monthrange(int(value.year), int(value.month))[1])

    def _shift_months(value: date, month_delta: int) -> date:
        month_index = (int(value.month) - 1) + int(month_delta)
        year = int(value.year) + (month_index // 12)
        month = (month_index % 12) + 1
        day = min(int(value.day), calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def _shift_recurring_token_date(base_date: date, offset: str | None) -> date:
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
            return _shift_months(base_date, 12 * amount)
        return _shift_months(base_date, amount)

    def _recurring_token_map(issue_date: date, *, offset: str | None = None) -> dict[str, str]:
        shifted_date = _shift_recurring_token_date(issue_date, offset)
        month_start = date(int(shifted_date.year), int(shifted_date.month), 1)
        month_end = _month_end(shifted_date)
        month_number = f"{int(shifted_date.month):02d}"
        month_name = STATS_MONTH_LABELS[int(shifted_date.month) - 1].lower()
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

    def _render_recurring_tokens(value: str | None, *, issue_date: date) -> str:
        text = str(value or "")
        token_pattern = re.compile(
            r"\{\{\s*(year|month|month_name|period_label|issue_date|month_start|month_end|period_start|period_end)\s*"
            r"(\+\+|--|[+-]\s*\d+\s*[dwmyDWMY]?)?\s*\}\}"
        )

        def replace_token(match: re.Match[str]) -> str:
            token_name = match.group(1)
            offset = match.group(2)
            token_map = _recurring_token_map(issue_date, offset=offset)
            return token_map.get(token_name, match.group(0))

        return token_pattern.sub(replace_token, text)

    def _default_invoice_footer_mode(subject: Subject | None) -> str:
        stored_mode = (getattr(subject, "default_invoice_footer_mode", None) or "").strip().lower()
        if stored_mode in {value for value, _label in INVOICE_FOOTER_PRESET_OPTIONS}:
            return stored_mode
        name = (getattr(subject, "name", None) or "").strip().lower()
        if any(token in name for token in (" z.s.", "z.s.", "spolek")):
            return "association_register"
        if any(token in name for token in ("s.r.o", " a.s.", " a. s.", "akciová společnost", "spol. s r.o", "k.s.", "v.o.s.", " družstvo")):
            return "commercial_register"
        return "trade_register"

    def _default_invoice_style(subject: Subject | None) -> str:
        theme_style = pdf_theme_to_invoice_style(getattr(subject, "invoice_pdf_theme", None))
        if theme_style == "modern":
            return _normalize_invoice_style(getattr(subject, "default_invoice_style", None) or theme_style)
        return _normalize_invoice_style(theme_style)

    def _invoice_footer_text_for_mode(
        mode: str | None,
        *,
        subject: Subject | None = None,
        language: str | None = None,
    ) -> str:
        normalized = (mode or "").strip().lower()
        if not normalized:
            normalized = _default_invoice_footer_mode(subject)
        if normalized == "custom":
            return str(getattr(subject, "default_invoice_footer_text", None) or "")
        lang = _normalize_invoice_language(language)
        localized_map = INVOICE_FOOTER_PRESET_TEXTS_I18N.get(lang) or INVOICE_FOOTER_PRESET_TEXTS_I18N["cs"]
        return str(localized_map.get(normalized, "") or "")

    def _resolve_invoice_footer(
        *,
        subject: Subject | None,
        footer_mode: str | None,
        footer_text: str | None,
        language: str | None = None,
    ) -> tuple[str, str]:
        normalized_mode = (footer_mode or "").strip().lower()
        if not normalized_mode:
            normalized_mode = _default_invoice_footer_mode(subject)
        custom_text = (footer_text or "").strip()
        if normalized_mode == "custom":
            if not custom_text:
                custom_text = _invoice_footer_text_for_mode(normalized_mode, subject=subject, language=language)
            return normalized_mode, custom_text
        if not custom_text and normalized_mode == "trade_register" and subject is not None:
            subject_default_mode = _default_invoice_footer_mode(subject)
            if subject_default_mode != "trade_register":
                normalized_mode = subject_default_mode
        resolved_text = _invoice_footer_text_for_mode(normalized_mode, subject=subject, language=language)
        return normalized_mode, resolved_text

    # ------------------------------------------------------------------
    # CSRF form helper (phase-29)
    # ------------------------------------------------------------------
    # Provide a small helper that templates can call to render a hidden
    # CSRF token field. The request instance is passed from FastAPI when
    # rendering templates. See `_ensure_csrf_token()` for token generation.
    def _csrf_input(request: Request) -> str:
        try:
            token = _ensure_csrf_token(request)
        except Exception:
            token = ""
        # Templates deliberately mark this small helper as safe, so keep the
        # dynamic value escaped even though generated tokens are currently hex.
        safe_token = escape(str(token), quote=True)
        return f'<input type="hidden" name="csrf_token" value="{safe_token}" />'

    templates.env.globals["csrf_input"] = _csrf_input

    def _ui_language_template_context(request: Request) -> dict[str, object]:
        language = normalize_ui_language(request.session.get("ui_language"))
        return {
            "ui_language": language,
            "current_ui_language": language,
            "ui_language_options": UI_LANGUAGE_OPTIONS,
            "ui_i18n_payload": ui_translation_payload(language),
            "ui_t": (lambda text: translate_ui_text(text, language)),
        }

    templates.context_processors.append(_ui_language_template_context)

    def _cents_to_amount_str(value_cents: int | None) -> str:
        """Convert integer cents to a stable "-12.34" string for form inputs."""

        try:
            v = int(value_cents or 0)
        except Exception:
            v = 0
        sign = "-" if v < 0 else ""
        v = abs(v)
        return f"{sign}{v // 100}.{v % 100:02d}"

    def _normalize_variable_symbol(value: str | None) -> str:
        return digits_only(value)

    def _contact_fixed_variable_symbol(contact: Contact | None) -> str:
        return _normalize_variable_symbol(getattr(contact, "fixed_variable_symbol", None))[:10]

    def _invoice_variable_symbol(invoice: Invoice | None, *, contact: Contact | None = None) -> str:
        stored = _normalize_variable_symbol(getattr(invoice, "variable_symbol", None))[:10]
        if stored:
            return stored
        fixed = _contact_fixed_variable_symbol(contact)
        if fixed:
            return fixed
        return variable_symbol_from_invoice_number(getattr(invoice, "number", None))

    def _invoice_taxable_supply_date(invoice: Invoice | None) -> date | None:
        value = getattr(invoice, "taxable_supply_date", None)
        if isinstance(value, date):
            return value
        issue = getattr(invoice, "issue_date", None)
        return issue if isinstance(issue, date) else None

    def _normalize_page_number(value: int | str | None) -> int:
        try:
            page = int(value or 1)
        except Exception:
            return 1
        return page if page > 0 else 1

    def _build_pagination_payload(
        request: Request,
        *,
        page: int,
        per_page: int,
        total_count: int,
        page_param: str = "page",
    ) -> dict[str, object]:
        total = max(0, int(total_count or 0))
        size = max(1, int(per_page or 50))
        page_count = max(1, ceil(total / size)) if total else 1
        current_page = min(max(1, int(page or 1)), page_count)
        offset = (current_page - 1) * size

        page_param_clean = str(page_param or "page").strip() or "page"
        query_pairs = [(k, v) for k, v in parse_qsl(str(request.url.query or ""), keep_blank_values=True) if k != page_param_clean]

        def page_url(target_page: int) -> str:
            params = list(query_pairs)
            if int(target_page) > 1:
                params.append((page_param_clean, str(int(target_page))))
            query = urlencode(params, doseq=True)
            return f"{request.url.path}?{query}" if query else str(request.url.path)

        start_page = max(1, current_page - 2)
        end_page = min(page_count, current_page + 2)
        if end_page - start_page < 4:
            if start_page == 1:
                end_page = min(page_count, start_page + 4)
            elif end_page == page_count:
                start_page = max(1, end_page - 4)

        return {
            "page": current_page,
            "per_page": size,
            "page_count": page_count,
            "total_count": total,
            "offset": offset,
            "limit": size,
            "from_item": (offset + 1) if total else 0,
            "to_item": min(total, offset + size),
            "prev_url": page_url(current_page - 1) if current_page > 1 else None,
            "next_url": page_url(current_page + 1) if current_page < page_count else None,
            "pages": [
                {
                    "number": page_no,
                    "url": page_url(page_no),
                    "current": page_no == current_page,
                }
                for page_no in range(start_page, end_page + 1)
            ],
        }

    INVOICE_DUE_TERM_OPTIONS: list[tuple[str, str]] = [
        ("7", "Týden"),
        ("14", "14 dní"),
        ("21", "21 dní"),
        ("30", "30 dní"),
        ("45", "45 dní"),
        ("60", "60 dní"),
    ]
    _invoice_due_term_values = {int(v) for v, _lbl in INVOICE_DUE_TERM_OPTIONS}
    INVOICE_ITEM_UNIT_OPTIONS: list[tuple[str, str]] = [
        ("", "Bez MJ"),
        ("hod", "Hodina"),
        ("ks", "Kus"),
        ("den", "Den"),
        ("měs", "Měsíc"),
        ("km", "Kilometr"),
        ("m", "Metr"),
        ("kg", "Kilogram"),
        ("paušál", "Paušál"),
    ]
    INVOICE_VAT_RATE_OPTIONS_BY_COUNTRY: dict[str, list[tuple[str, str]]] = {
        "CZ": [("21", "21 % - základní"), ("12", "12 % - snížená"), ("0", "0 %")],
        "SK": [("23", "23 % - základná"), ("19", "19 % - znížená"), ("5", "5 % - znížená"), ("0", "0 %")],
    }

    def _invoice_subject_country(subject: object | None = None) -> str:
        country = str(getattr(subject, "country", None) or settings.issuer_country or "CZ").strip().upper()
        return country if len(country) == 2 else "CZ"

    def _invoice_vat_rate_options(country: str | None) -> list[tuple[str, str]]:
        country_key = str(country or "CZ").strip().upper() or "CZ"
        return list(INVOICE_VAT_RATE_OPTIONS_BY_COUNTRY.get(country_key) or INVOICE_VAT_RATE_OPTIONS_BY_COUNTRY["CZ"])

    def _invoice_default_vat_rate(country: str | None) -> str:
        options = _invoice_vat_rate_options(country)
        return options[0][0] if options else "21"

    def _decimal_to_input_str(value: Decimal | None, *, default: str = "") -> str:
        """Render Decimal values in a stable form for HTML inputs."""

        if value is None:
            return default
        try:
            out = format(Decimal(value), "f")
        except Exception:
            return default
        if "." in out:
            out = out.rstrip("0").rstrip(".")
        return out or default

    def _normalize_invoice_item_unit(value: str | None) -> str:
        unit = " ".join(str(value or "").split()).strip()
        return unit[:32]

    def _blank_invoice_item_prefill(*, is_vat_payer: bool, default_vat_rate: str = "21") -> dict[str, str]:
        return {
            "description": "",
            "quantity": "1",
            "unit": "",
            "unit_price": "",
            "vat_rate": default_vat_rate if is_vat_payer else "0",
            "total_preview": "",
        }

    def _invoice_item_prefill_from_model(item: object, *, is_vat_payer: bool, default_vat_rate: str = "21") -> dict[str, str]:
        vat_default = default_vat_rate if is_vat_payer else "0"
        return {
            "description": str(getattr(item, "description", "") or ""),
            "quantity": _decimal_to_input_str(getattr(item, "quantity", None), default="1"),
            "unit": _normalize_invoice_item_unit(getattr(item, "unit", "")),
            "unit_price": _cents_to_amount_str(getattr(item, "unit_price_cents", 0)),
            "vat_rate": _decimal_to_input_str(getattr(item, "vat_rate", None), default=vat_default),
            "total_preview": "",
        }

    def _is_blank_invoice_item_row(raw: dict[str, str], *, is_vat_payer: bool, default_vat_rate: str = "21") -> bool:
        description = str(raw.get("description") or "").strip()
        quantity = str(raw.get("quantity") or "").strip()
        unit_price = str(raw.get("unit_price") or "").strip()
        vat_rate = str(raw.get("vat_rate") or "").strip()

        if description:
            return False
        if unit_price:
            return False
        if quantity not in {"", "1", "1.0", "1.00"}:
            return False
        if is_vat_payer:
            default_vat_variants = {"", default_vat_rate, f"{default_vat_rate}.0", f"{default_vat_rate}.00"}
            if vat_rate not in default_vat_variants:
                return False
        else:
            if vat_rate not in {"", "0", "0.0", "0.00"}:
                return False
        return True

    def _preview_invoice_item_total_cents(
        raw: dict[str, str],
        *,
        is_vat_payer: bool,
        allow_negative_unit_price: bool = False,
        default_vat_rate: str = "21",
    ) -> int:
        if _is_blank_invoice_item_row(raw, is_vat_payer=is_vat_payer, default_vat_rate=default_vat_rate):
            return 0
        try:
            quantity = parse_quantity(raw.get("quantity"))
            if allow_negative_unit_price:
                signed_unit_price_cents = parse_money_to_signed_cents(raw.get("unit_price"))
                unit_price_sign = -1 if signed_unit_price_cents < 0 else 1
                unit_price_cents = abs(int(signed_unit_price_cents))
            else:
                unit_price_sign = 1
                unit_price_cents = parse_money_to_cents(raw.get("unit_price"))
            vat_rate = parse_vat_rate(raw.get("vat_rate")) if is_vat_payer else Decimal("0.00")
            _net, _vat, total = compute_line_amounts_cents(
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                vat_rate=vat_rate,
            )
            return int(total) * int(unit_price_sign)
        except Exception:
            return 0

    def _prepare_invoice_item_prefill_rows(
        rows: list[dict[str, str]] | None,
        *,
        currency: str,
        is_vat_payer: bool,
        allow_negative_unit_price: bool = False,
        min_rows: int = 1,
        default_vat_rate: str = "21",
    ) -> tuple[list[dict[str, str]], int]:
        prepared: list[dict[str, str]] = []
        items_total_cents = 0

        for row in list(rows or []):
            raw = {
                "description": str(row.get("description") or ""),
                "quantity": str(row.get("quantity") or "1"),
                "unit": _normalize_invoice_item_unit(row.get("unit")),
                "unit_price": str(row.get("unit_price") or ""),
                "vat_rate": str(row.get("vat_rate") or (default_vat_rate if is_vat_payer else "0")),
            }
            total_cents = _preview_invoice_item_total_cents(
                raw,
                is_vat_payer=is_vat_payer,
                allow_negative_unit_price=allow_negative_unit_price,
                default_vat_rate=default_vat_rate,
            )
            raw["total_preview"] = format_cents(total_cents, currency)
            prepared.append(raw)
            items_total_cents += int(total_cents)

        while len(prepared) < max(1, int(min_rows)):
            blank = _blank_invoice_item_prefill(is_vat_payer=is_vat_payer, default_vat_rate=default_vat_rate)
            blank["total_preview"] = format_cents(0, currency)
            prepared.append(blank)

        return prepared, int(items_total_cents)

    def _parse_invoice_items_from_form(
        form,
        *,
        is_vat_payer: bool,
        allow_negative_unit_price: bool = False,
        default_vat_rate: str = "21",
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        descriptions = [str(v or "") for v in form.getlist("item_description")]
        quantities = [str(v or "") for v in form.getlist("item_quantity")]
        units = [str(v or "") for v in form.getlist("item_unit")]
        unit_prices = [str(v or "") for v in form.getlist("item_unit_price")]
        vat_rates = [str(v or "") for v in form.getlist("item_vat_rate")]

        row_count = max(len(descriptions), len(quantities), len(units), len(unit_prices), len(vat_rates), 1)

        parsed_items: list[dict[str, object]] = []
        prefill_rows: list[dict[str, str]] = []

        for index in range(row_count):
            raw = {
                "description": descriptions[index].strip() if index < len(descriptions) else "",
                "quantity": quantities[index].strip() if index < len(quantities) else "1",
                "unit": _normalize_invoice_item_unit(units[index] if index < len(units) else ""),
                "unit_price": unit_prices[index].strip() if index < len(unit_prices) else "",
                "vat_rate": vat_rates[index].strip() if index < len(vat_rates) else (default_vat_rate if is_vat_payer else "0"),
            }
            prefill_rows.append(dict(raw))

            if _is_blank_invoice_item_row(raw, is_vat_payer=is_vat_payer, default_vat_rate=default_vat_rate):
                continue

            if not raw["description"]:
                raise ValueError(f"Položka #{index + 1}: vyplň popis.")

            try:
                quantity = parse_quantity(raw.get("quantity"))
            except ValueError as exc:
                raise ValueError(f"Položka #{index + 1}: {str(exc)}") from exc

            try:
                if allow_negative_unit_price:
                    signed_unit_price_cents = parse_money_to_signed_cents(raw.get("unit_price"))
                    unit_price_sign = -1 if signed_unit_price_cents < 0 else 1
                    unit_price_cents = abs(int(signed_unit_price_cents))
                else:
                    unit_price_sign = 1
                    unit_price_cents = parse_money_to_cents(raw.get("unit_price"))
            except ValueError as exc:
                raise ValueError(f"Položka #{index + 1}: {str(exc)}") from exc

            try:
                vat_rate = parse_vat_rate(raw.get("vat_rate")) if is_vat_payer else Decimal("0.00")
            except ValueError as exc:
                raise ValueError(f"Položka #{index + 1}: {str(exc)}") from exc

            line_net_cents, line_vat_cents, line_total_cents = compute_line_amounts_cents(
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                vat_rate=vat_rate,
            )
            line_net_cents *= int(unit_price_sign)
            line_vat_cents *= int(unit_price_sign)
            line_total_cents *= int(unit_price_sign)
            unit_price_cents *= int(unit_price_sign)

            parsed_items.append(
                {
                    "description": raw["description"],
                    "quantity": quantity,
                    "unit": raw["unit"],
                    "unit_price_cents": int(unit_price_cents),
                    "vat_rate": vat_rate,
                    "line_net_cents": int(line_net_cents),
                    "line_vat_cents": int(line_vat_cents),
                    "line_total_cents": int(line_total_cents),
                }
            )

        return parsed_items, prefill_rows

    def _replace_invoice_items(
        db: Session,
        *,
        invoice_id: int,
        items_payload: list[dict[str, object]],
    ) -> None:
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
                    unit=_normalize_invoice_item_unit(str(payload.get("unit") or "")),
                    unit_price_cents=int(payload["unit_price_cents"]),
                    vat_rate=payload["vat_rate"],
                    line_net_cents=int(payload["line_net_cents"]),
                    line_vat_cents=int(payload["line_vat_cents"]),
                    line_total_cents=int(payload["line_total_cents"]),
                    sort_order=idx,
                )
            )
        db.flush()

    def _catalog_signature_key(entry: dict[str, object]) -> tuple[str, str, str, int, str]:
        return (
            " ".join(str(entry.get("description", "") or "").split()).strip().casefold(),
            str(entry.get("quantity", "1") or "1"),
            _normalize_invoice_item_unit(entry.get("unit")),
            int(entry.get("unit_price_cents", 0) or 0),
            str(entry.get("vat_rate", "0") or "0"),
        )

    def _serialize_invoice_catalog_item(item: InvoiceCatalogItem) -> dict[str, object]:
        description = " ".join(str(getattr(item, "description", "") or "").split()).strip()
        currency = str(getattr(item, "currency", "CZK") or "CZK").strip().upper() or "CZK"
        quantity = _decimal_to_input_str(getattr(item, "quantity", None), default="1")
        unit = _normalize_invoice_item_unit(getattr(item, "unit", ""))
        unit_price_cents = int(getattr(item, "unit_price_cents", 0) or 0)
        vat_rate = _decimal_to_input_str(getattr(item, "vat_rate", None), default="0")
        updated_at = getattr(item, "updated_at", None)
        return {
            "id": int(getattr(item, "id", 0) or 0),
            "description": description,
            "quantity": quantity,
            "unit": unit,
            "unit_price_cents": unit_price_cents,
            "unit_price": _cents_to_amount_str(unit_price_cents),
            "unit_price_preview": format_cents(unit_price_cents, currency),
            "vat_rate": vat_rate,
            "currency": currency,
            "invoice_number": "",
            "last_used_on": updated_at.date().isoformat() if isinstance(updated_at, datetime) else "",
            "usage_count": 1,
            "source": "catalog",
        }

    def _list_invoice_catalog_items(
        db: Session,
        *,
        subject_id: int,
        query: str = "",
        limit: int = 12,
        currency: str | None = None,
    ) -> list[dict[str, object]]:
        query_clean = " ".join(str(query or "").split()).strip()
        limit = max(1, min(int(limit or 12), 30))
        currency_clean = str(currency or "").strip().upper()
        if len(currency_clean) != 3:
            currency_clean = ""

        stmt = select(InvoiceCatalogItem).where(InvoiceCatalogItem.subject_id == int(subject_id))

        if currency_clean:
            stmt = stmt.where(InvoiceCatalogItem.currency == currency_clean)

        if query_clean:
            stmt = stmt.where(func.lower(InvoiceCatalogItem.description).like(f"%{query_clean.lower()}%"))

        stmt = stmt.order_by(InvoiceCatalogItem.updated_at.desc(), InvoiceCatalogItem.id.desc()).limit(limit)

        try:
            rows = db.scalars(stmt).all()
        except SQLAlchemyError:
            return []

        return [_serialize_invoice_catalog_item(item) for item in rows]

    def _save_invoice_catalog_item(
        db: Session,
        *,
        subject_id: int,
        description: str | None,
        quantity: str | None,
        unit: str | None,
        unit_price: str | None,
        vat_rate: str | None,
        currency: str | None,
        is_vat_payer: bool,
    ) -> tuple[dict[str, object], bool]:
        description_clean = " ".join(str(description or "").split()).strip()
        if not description_clean:
            raise ValueError("Vyplň popis položky, kterou chceš uložit do katalogu.")

        quantity_value = parse_quantity(quantity)
        unit_value = _normalize_invoice_item_unit(unit)
        unit_price_cents = parse_money_to_cents(unit_price)
        vat_rate_value = parse_vat_rate(vat_rate) if is_vat_payer else Decimal("0.00")
        currency_clean = str(currency or "").strip().upper() or "CZK"
        if len(currency_clean) != 3:
            currency_clean = "CZK"

        existing = db.scalar(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .where(InvoiceCatalogItem.currency == currency_clean)
            .where(func.lower(func.trim(InvoiceCatalogItem.description)) == description_clean.casefold())
            .where(InvoiceCatalogItem.quantity == quantity_value)
            .where(InvoiceCatalogItem.unit == unit_value)
            .where(InvoiceCatalogItem.unit_price_cents == int(unit_price_cents))
            .where(InvoiceCatalogItem.vat_rate == vat_rate_value)
            .order_by(InvoiceCatalogItem.updated_at.desc(), InvoiceCatalogItem.id.desc())
        )

        created = existing is None
        item = existing or InvoiceCatalogItem(subject_id=int(subject_id))
        item.description = description_clean
        item.quantity = quantity_value
        item.unit = unit_value
        item.unit_price_cents = int(unit_price_cents)
        item.vat_rate = vat_rate_value
        item.currency = currency_clean
        db.add(item)
        db.flush()
        return _serialize_invoice_catalog_item(item), created

    def _list_invoice_item_suggestions(
        db: Session,
        *,
        subject_id: int,
        query: str = "",
        limit: int = 8,
        currency: str | None = None,
        exclude_invoice_id: int | None = None,
    ) -> list[dict[str, object]]:
        query_clean = " ".join(str(query or "").split()).strip()
        limit = max(1, min(int(limit or 8), 20))
        currency_clean = str(currency or "").strip().upper()
        if len(currency_clean) != 3:
            currency_clean = ""

        catalog_limit = min(limit, 4) if not query_clean else limit
        catalog_suggestions = _list_invoice_catalog_items(
            db,
            subject_id=subject_id,
            query=query_clean,
            limit=catalog_limit,
            currency=currency_clean or None,
        )

        stmt = (
            select(InvoiceItem, Invoice)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(Invoice.subject_id == int(subject_id))
            .where(func.trim(InvoiceItem.description) != "")
        )

        if exclude_invoice_id is not None and int(exclude_invoice_id) > 0:
            stmt = stmt.where(Invoice.id != int(exclude_invoice_id))

        if currency_clean:
            stmt = stmt.where(Invoice.currency == currency_clean)

        if query_clean:
            stmt = stmt.where(func.lower(InvoiceItem.description).like(f"%{query_clean.lower()}%"))

        stmt = stmt.order_by(
            *_invoice_newest_first_ordering(),
            InvoiceItem.sort_order.asc(),
            InvoiceItem.id.asc(),
        ).limit(limit * 20)

        try:
            rows = db.execute(stmt).all()
        except SQLAlchemyError:
            rows = []

        grouped: dict[tuple[str, str, str, int, str], dict[str, object]] = {}

        for item, invoice in rows:
            description = " ".join(str(getattr(item, "description", "") or "").split()).strip()
            if not description:
                continue

            quantity = _decimal_to_input_str(getattr(item, "quantity", None), default="1")
            unit = _normalize_invoice_item_unit(getattr(item, "unit", ""))
            vat_rate = _decimal_to_input_str(getattr(item, "vat_rate", None), default="0")
            unit_price_cents = int(getattr(item, "unit_price_cents", 0) or 0)
            description_cf = description.casefold()
            key = (description_cf, quantity, unit, unit_price_cents, vat_rate)

            issue_date = getattr(invoice, "issue_date", None)
            issue_sort = issue_date.toordinal() if isinstance(issue_date, date) else 0

            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "description": description,
                    "description_cf": description_cf,
                    "quantity": quantity,
                    "unit": unit,
                    "unit_price_cents": unit_price_cents,
                    "unit_price": _cents_to_amount_str(unit_price_cents),
                    "unit_price_preview": format_cents(unit_price_cents, str(getattr(invoice, "currency", "CZK") or "CZK")),
                    "vat_rate": vat_rate,
                    "currency": str(getattr(invoice, "currency", "CZK") or "CZK"),
                    "invoice_number": str(getattr(invoice, "number", "") or ""),
                    "last_used_on": issue_date.isoformat() if isinstance(issue_date, date) else "",
                    "usage_count": 1,
                    "_issue_sort": issue_sort,
                    "_invoice_sort": int(getattr(invoice, "id", 0) or 0),
                    "source": "history",
                }
            else:
                entry["usage_count"] = int(entry.get("usage_count", 1)) + 1
                if issue_sort > int(entry.get("_issue_sort", 0)):
                    entry["invoice_number"] = str(getattr(invoice, "number", "") or "")
                    entry["last_used_on"] = issue_date.isoformat() if isinstance(issue_date, date) else ""
                    entry["_issue_sort"] = issue_sort
                    entry["_invoice_sort"] = int(getattr(invoice, "id", 0) or 0)
                    entry["currency"] = str(getattr(invoice, "currency", "CZK") or "CZK")
                    entry["unit_price_preview"] = format_cents(unit_price_cents, str(getattr(invoice, "currency", "CZK") or "CZK"))

        query_cf = query_clean.casefold()
        history_suggestions = list(grouped.values())

        def _sort_key(entry: dict[str, object]) -> tuple[object, ...]:
            description_cf = str(entry.get("description_cf", ""))
            if query_cf:
                exact_rank = 0 if description_cf == query_cf else 1
                prefix_rank = 0 if description_cf.startswith(query_cf) else 1
                contains_pos = description_cf.find(query_cf)
                if contains_pos < 0:
                    contains_pos = 9999
            else:
                exact_rank = 1
                prefix_rank = 1
                contains_pos = 0
            return (
                exact_rank,
                prefix_rank,
                contains_pos,
                -int(entry.get("_issue_sort", 0)),
                -int(entry.get("_invoice_sort", 0)),
                -int(entry.get("usage_count", 0)),
                description_cf,
            )

        history_suggestions.sort(key=_sort_key)

        combined: list[dict[str, object]] = []
        seen: set[tuple[str, str, str, int, str]] = set()
        for entry in [*catalog_suggestions, *history_suggestions]:
            key = _catalog_signature_key(entry)
            if key in seen:
                continue
            seen.add(key)
            combined.append(
                {
                    "id": int(entry.get("id", 0) or 0),
                    "description": str(entry.get("description", "")),
                    "quantity": str(entry.get("quantity", "1")),
                    "unit": _normalize_invoice_item_unit(entry.get("unit")),
                    "unit_price_cents": int(entry.get("unit_price_cents", 0) or 0),
                    "unit_price": str(entry.get("unit_price", "")),
                    "unit_price_preview": str(entry.get("unit_price_preview", "")),
                    "vat_rate": str(entry.get("vat_rate", "0")),
                    "currency": str(entry.get("currency", currency_clean or "CZK")),
                    "invoice_number": str(entry.get("invoice_number", "")),
                    "last_used_on": str(entry.get("last_used_on", "")),
                    "usage_count": int(entry.get("usage_count", 1) or 1),
                    "source": str(entry.get("source", "history") or "history"),
                }
            )
            if len(combined) >= limit:
                break

        return combined

    def _infer_due_term_value(issue_date: date | None, due_date: date | None) -> str:
        if issue_date is None or due_date is None:
            return "14"
        delta = (due_date - issue_date).days
        if delta in _invoice_due_term_values:
            return str(delta)
        return "custom"

    def _apply_invoice_editor_summary(
        *,
        prefill: dict,
        prefill_items: list[dict[str, str]] | None,
        is_vat_payer: bool,
        allow_negative_unit_price: bool = False,
        min_rows: int = 1,
        default_vat_rate: str = "21",
    ) -> tuple[dict, list[dict[str, str]]]:
        currency = str(prefill.get("currency") or "CZK").strip().upper() or "CZK"
        prefill["currency"] = currency
        prepared_items, items_total_cents = _prepare_invoice_item_prefill_rows(
            prefill_items,
            currency=currency,
            is_vat_payer=is_vat_payer,
            allow_negative_unit_price=allow_negative_unit_price,
            min_rows=min_rows,
            default_vat_rate=default_vat_rate,
        )

        discount_raw = str(prefill.get("discount_amount") or "").strip()
        discount_cents = 0
        discount_input_value = discount_raw
        if discount_raw:
            try:
                discount_cents = parse_money_to_cents(discount_raw)
                discount_input_value = _cents_to_amount_str(discount_cents) if discount_cents != 0 else ""
            except ValueError:
                discount_cents = 0
        discount_preview_cents = min(discount_cents, max(items_total_cents, 0))
        subtotal_after_discount_cents = int(items_total_cents - discount_preview_cents)

        rounding_enabled = bool(prefill.get("rounding_enabled")) and str(prefill.get("rounding_enabled")).lower() not in {"0", "false", "off", ""}
        if rounding_enabled:
            rounding_cents = compute_rounding_adjustment_cents(subtotal_after_discount_cents)
            prefill["rounding_adjustment"] = _cents_to_amount_str(rounding_cents) if rounding_cents != 0 else "0.00"
        else:
            try:
                rounding_cents = parse_money_to_signed_cents(prefill.get("rounding_adjustment"))
            except ValueError:
                rounding_cents = 0

        prefill["discount_amount"] = discount_input_value
        prefill["rounding_enabled"] = bool(rounding_enabled)
        prefill["items_total_preview"] = format_cents(items_total_cents, currency)
        prefill["discount_preview"] = format_cents(-discount_preview_cents, currency)
        prefill["subtotal_after_discount_preview"] = format_cents(subtotal_after_discount_cents, currency)
        prefill["rounding_preview"] = format_cents(rounding_cents, currency)
        prefill["total_preview"] = format_cents(subtotal_after_discount_cents + rounding_cents, currency)
        prefill["zero_money_preview"] = format_cents(0, currency)
        prefill["final_number_preview"] = str(prefill.get("final_number_preview") or "Přidělí se při vystavení")
        if not prefill.get("due_term"):
            try:
                prefill["due_term"] = _infer_due_term_value(
                    date.fromisoformat(str(prefill.get("issue_date") or "")),
                    date.fromisoformat(str(prefill.get("due_date") or "")),
                )
            except Exception:
                prefill["due_term"] = "14"
        return prefill, prepared_items

    # Note: keep /static mounted separately; PDFs are stored outside webroot.
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _app_css_cache: str | None = None

    def _load_app_css() -> str:
        """Load static/app.css for PDF rendering.

        For WeasyPrint (HTML → PDF) we prefer to inline CSS to avoid any network
        fetching during PDF generation.
        """

        nonlocal _app_css_cache
        if _app_css_cache is not None:
            return _app_css_cache
        try:
            css_path = static_dir / "app.css"
            _app_css_cache = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
        except Exception:
            _app_css_cache = ""
        return _app_css_cache

    # ------------------------------------------------------------------
    # Import file helpers (phase-24)
    # ------------------------------------------------------------------

    def _safe_resolve_under_root(root: Path, relpath: Path) -> Path:
        """Resolve a relative path under a storage root and prevent traversal."""

        r = root.resolve()
        p = Path(relpath)
        if p.is_absolute():
            raise ValueError("path must be relative")
        full = (r / p).resolve()
        if full == r or r in full.parents:
            return full
        raise ValueError("path escapes storage root")

    def _import_file_relpath(*, subject_id: int, run_id: int, original_filename: str) -> Path:
        """Build a safe relative path for an uploaded import file."""

        name = (original_filename or "").strip()
        # Keep only the last path component in case the client sent a full path.
        base_name = Path(name).name if name else "import"
        stem = Path(base_name).stem
        ext = (Path(base_name).suffix or "").lower()
        # Avoid overly long / weird extensions.
        if len(ext) > 10 or any(ch for ch in ext if not (ch.isalnum() or ch in {".", "_", "-"})):
            ext = ""
        if not ext:
            ext = ".bin"

        safe_base = safe_filename_base(stem, fallback=f"import-{int(run_id)}")
        return Path(f"subject-{int(subject_id)}") / f"run-{int(run_id)}-{safe_base}{ext}"

    async def _save_upload_to_temp(
        upload,
        *,
        max_bytes: int,
    ) -> tuple[Path, str, int]:
        """Stream UploadFile into a temp file while computing sha256.

        Returns: (tmp_path, sha256_hex, size_bytes)
        """

        h = hashlib.sha256()
        size = 0

        fd, tmp_name = tempfile.mkstemp(prefix=".fakturek-upload-", suffix=".tmp")
        tmp_path = Path(tmp_name)

        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes and size > int(max_bytes):
                        raise ValueError("Soubor je příliš velký")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
            raise

        return tmp_path, h.hexdigest(), int(size)

    def _persist_uploaded_temp_file(tmp_path: Path, destination: Path) -> None:
        """Atomically persist an upload, including across filesystem boundaries."""

        try:
            os.replace(tmp_path, destination)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise

        staged = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
        try:
            with tmp_path.open("rb") as source, staged.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(staged, destination)
            try:
                tmp_path.unlink()
            except OSError:
                pass
        except Exception:
            staged.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Current request context (phase-14)
    # ------------------------------------------------------------------
    #
    # Store the current request in a ContextVar so helper functions (such
    # as _current_subject_id) can inspect the session without explicitly
    # passing the request object.  This is reset after each request.
    _current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
        "_current_request", default=None
    )

    class CurrentRequestMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            token = _current_request.set(request)
            try:
                return await call_next(request)
            finally:
                _current_request.reset(token)

    # Install before auth so it's available everywhere.
    app.add_middleware(CurrentRequestMiddleware)


    TECHNICAL_PUBLIC_PATHS = {
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/robots.txt",
        "/sitemap.xml",
        "/ads.txt",
        "/browserconfig.xml",
        "/manifest.json",
        "/site.webmanifest",
    }

    def _is_technical_public_path(path: str | None) -> bool:
        clean_path = str(path or "").split("?", 1)[0].strip()
        return clean_path in TECHNICAL_PUBLIC_PATHS

    def _is_safe_navigation_next_path(path: str | None) -> bool:
        clean_path = str(path or "").split("?", 1)[0].strip()
        if not clean_path:
            return False
        if _is_technical_public_path(clean_path):
            return False
        if clean_path.startswith("/static/"):
            return False
        return True

    def _safe_next_url(next_url: str | None, default: str = "/") -> str:
        """Return a safe redirect target.

        Accept only relative paths starting with `/`.
        """

        if not next_url:
            return default
        u = str(next_url).strip()
        if "\\" in u or any(ord(char) < 0x20 or ord(char) == 0x7F for char in u):
            return default
        try:
            parsed = urlsplit(u)
        except ValueError:
            return default
        if (
            u.startswith("/")
            and not u.startswith("//")
            and not parsed.scheme
            and not parsed.netloc
            and _is_safe_navigation_next_path(u)
        ):
            return u
        return default

    def _with_query_params(next_url: str | None, **updates: str | int | None) -> str:
        """Return a safe relative URL with merged query parameters."""

        target = _safe_next_url(next_url, "/")
        parts = urlsplit(target)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in updates.items():
            if value is None:
                query.pop(str(key), None)
            else:
                query[str(key)] = str(value)
        return urlunsplit(("", "", parts.path or "/", urlencode(query), parts.fragment))

    def _with_saved_flag(next_url: str | None, *, fallback: str) -> str:
        return _with_query_params(_safe_next_url(next_url, fallback), saved="1")

    def _internal_job_token_value() -> str:
        return str(getattr(settings, "internal_job_token", "") or "").strip()

    def _verify_internal_job_request(request: Request) -> None:
        expected = _internal_job_token_value()
        if not expected:
            raise HTTPException(status_code=503, detail="Internal job token is not configured")

        provided = str(request.headers.get("x-internal-job-token") or "").strip()
        if not provided:
            auth_header = str(request.headers.get("authorization") or "").strip()
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()

        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="Access denied")

    def _subject_switch_token_for_request(request: Request, *, subject_id: int, next_url: str | None) -> str:
        base_token = _ensure_csrf_token(request)
        payload = f"{int(subject_id)}:{_safe_next_url(next_url, '/')}"
        return hashlib.sha256(f"{base_token}:{payload}".encode("utf-8")).hexdigest()

    def _subject_switch_url(request: Request, *, subject_id: int, next_url: str | None) -> str:
        safe_next = _safe_next_url(next_url, "/")
        if not settings.auth_required:
            return f"/subjects/{int(subject_id)}/switch?next={quote(safe_next, safe='')}"
        switch_token = _subject_switch_token_for_request(
            request,
            subject_id=int(subject_id),
            next_url=safe_next,
        )
        return (
            f"/subjects/{int(subject_id)}/switch"
            f"?next={quote(safe_next, safe='')}&st={quote(switch_token, safe='')}"
        )

    def _subject_switch_form_values(request: Request, *, subject_id: int, next_url: str | None) -> dict[str, str]:
        safe_next = _safe_next_url(next_url, "/")
        values = {"next": safe_next}
        if settings.auth_required:
            values["st"] = _subject_switch_token_for_request(
                request,
                subject_id=int(subject_id),
                next_url=safe_next,
            )
        return values

    def _topbar_subject_switch_target(request: Request) -> str:
        raw_path = _safe_next_url(_request_scope_path(request), "/")
        query = _request_scope_query(request)

        if raw_path.startswith("/invoices/"):
            if raw_path not in {"/invoices", "/invoices/new"} and not raw_path.startswith("/invoices/export"):
                return "/invoices"
        if raw_path.startswith("/contacts/"):
            if raw_path not in {"/contacts", "/contacts/new"} and not raw_path.startswith("/contacts/export"):
                return "/contacts"

        target = raw_path or "/"
        if query and target in {"/", "/invoices", "/contacts", "/stats", "/imports", "/settings"}:
            return f"{target}?{query}"
        return target

    def _request_wants_json(request: Request) -> bool:
        accept_header = (request.headers.get("accept") or "").lower()
        return "application/json" in accept_header and "text/html" not in accept_header


    class _AuthRequiredMiddleware(BaseHTTPMiddleware):
        """Require an authenticated session for non-public pages.

        Kept opt-in in development via `AUTH_REQUIRED`.
        """

        async def dispatch(self, request: Request, call_next):
            if not settings.auth_required:
                return await call_next(request)

            path = _request_scope_path(request)

            # Always-public endpoints.
            if _is_technical_public_path(path):
                return await call_next(request)
            if path.startswith("/static/"):
                return await call_next(request)
            if path.startswith("/internal/jobs/"):
                return await call_next(request)
            if path.startswith("/api/v1"):
                return await call_next(request)
            if path in {
                "/healthz",
                "/healthz/db",
                "/login",
                "/logout",
                "/password/reset",
                "/setup",
                "/signup",
                "/signup/pending",
                "/signup/verify",
                "/signup/resend-verification",
            }:
                return await call_next(request)

            if _is_public_invoice_path(path):
                return await call_next(request)

            if request.session.get("user_id"):
                if _db_enabled:
                    invalid_reason = None
                    try:
                        user_id = int(request.session.get("user_id"))
                        session_version = int(request.session.get("session_version") or 0)
                        authenticated_at_raw = str(request.session.get("authenticated_at") or "")
                        authenticated_at = as_utc_aware(datetime.fromisoformat(authenticated_at_raw)) if authenticated_at_raw else None
                        db_gen = get_db()
                        db = next(db_gen)
                        try:
                            user = db.get(User, int(user_id))
                            if user is None or not bool(getattr(user, "is_active", False)):
                                invalid_reason = "invalid-user"
                            elif int(getattr(user, "session_version", 1) or 1) != session_version:
                                invalid_reason = "session-version"
                            else:
                                max_days = _normalize_session_max_age_days(getattr(user, "session_max_age_days", 7))
                                if authenticated_at is None or utc_now() - authenticated_at > timedelta(days=max_days):
                                    invalid_reason = "session-expired"
                        finally:
                            try:
                                next(db_gen)
                            except StopIteration:
                                pass
                    except Exception:
                        logging.getLogger("fakturek").exception(
                            "Session validation failed"
                        )
                        return JSONResponse(
                            status_code=503,
                            content={"detail": "Session validation is temporarily unavailable"},
                            headers={"Retry-After": "5"},
                        )
                    if invalid_reason:
                        request.session.clear()
                        if request.method in {"GET", "HEAD"}:
                            next_target = path
                            query = _request_scope_query(request)
                            if query:
                                next_target = f"{next_target}?{query}"
                            return RedirectResponse(url=f"/login?next={quote(next_target)}&expired=1", status_code=303)
                        return JSONResponse(status_code=401, content={"detail": "Session expired"})
                return await call_next(request)

            # Not authenticated.
            if request.method in {"GET", "HEAD"}:
                next_target = path
                query = _request_scope_query(request)
                if query:
                    next_target = f"{next_target}?{query}"
                return RedirectResponse(
                    url=f"/login?next={quote(next_target)}",
                    status_code=303,
                )

            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    app.add_middleware(_AuthRequiredMiddleware)

    VAT_REGISTRATION_THRESHOLDS = (
        ("První limit", 2_000_000 * 100),
        ("Druhý limit", 2_536_500 * 100),
    )
    FLAT_TAX_THRESHOLD_MATRIX: dict[str, dict[str, int]] = {
        "1": {
            "general": 1_000_000 * 100,
            "mostly_80_60": 1_500_000 * 100,
            "mostly_80": 2_000_000 * 100,
        },
        "2": {
            "general": 1_500_000 * 100,
            "mostly_80_60": 2_000_000 * 100,
            "mostly_80": 2_000_000 * 100,
        },
        "3": {
            "general": 2_000_000 * 100,
            "mostly_80_60": 2_000_000 * 100,
            "mostly_80": 2_000_000 * 100,
        },
    }
    TAX_ALERT_TRIGGER_STAGES: list[tuple[int, int, str]] = [
        (1, 80, "na 80 % limitu"),
        (2, 90, "na 90 % limitu"),
        (3, 100, "na limitu"),
    ]
    FLAT_TAX_PROFILE_NOTES: dict[str, str] = {
        "general": "Bez převahy činností s 80% nebo 60% výdajovým paušálem.",
        "mostly_80_60": "Aspoň 75 % příjmů spadá do činností s 80% nebo 60% výdajovým paušálem.",
        "mostly_80": "Aspoň 75 % příjmů spadá do činností s 80% výdajovým paušálem.",
    }

    def _flat_tax_band_limit_cents(*, band: str | None, income_profile: str | None) -> int:
        normalized_band = _normalize_flat_tax_band(band)
        normalized_profile = _normalize_flat_tax_income_profile(income_profile)
        return int(
            FLAT_TAX_THRESHOLD_MATRIX.get(normalized_band, FLAT_TAX_THRESHOLD_MATRIX["1"]).get(
                normalized_profile,
                FLAT_TAX_THRESHOLD_MATRIX["1"]["general"],
            )
        )

    def _flat_tax_thresholds_for_profile(*, income_profile: str | None) -> list[dict[str, object]]:
        normalized_profile = _normalize_flat_tax_income_profile(income_profile)
        rows: list[dict[str, object]] = []
        for band, _label in FLAT_TAX_BAND_OPTIONS:
            rows.append(
                {
                    "band": band,
                    "title": f"{band}. pásmo",
                    "amount_cents": _flat_tax_band_limit_cents(band=band, income_profile=normalized_profile),
                }
            )
        return rows

    def _tax_alert_stage_for_turnover(*, turnover_cents: int, limit_cents: int) -> tuple[int, str]:
        if int(limit_cents or 0) <= 0:
            return 0, ""
        turnover = int(turnover_cents or 0)
        limit = int(limit_cents or 0)
        for stage, percent, label in reversed(TAX_ALERT_TRIGGER_STAGES):
            trigger_amount = (limit * percent + 99) // 100
            if turnover >= trigger_amount:
                return int(stage), str(label)
        return 0, ""

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        context: dict[str, object] = {
            "app_env": settings.app_env,
            "db_enabled": _db_enabled,
            "vat_limit": None,
            "flat_tax_limit": None,
            "recent_invoices": [],
            "yearly_revenue_overview": [],
            "chart_currency": "CZK",
            "recurring_plans_overview": [],
            "top_clients": [],
            "outstanding_summary": {
                "open_count": 0,
                "overdue_count": 0,
                "open_total_cents": 0,
                "currency": "CZK",
            },
        }
        if _db_enabled:
            try:
                from fakturek.db import get_sessionmaker  # type: ignore

                SessionLocal = get_sessionmaker()
                with SessionLocal() as db:  # type: ignore
                    context.update(_build_home_vat_limit_context(db))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                context.update(
                    {
                        "db_enabled": False,
                        "db_error": _safe_db_error_message(exc),
                    }
                )
            except Exception as exc:  # pragma: no cover
                context.update(
                    {
                        "db_enabled": False,
                        "db_error": _safe_db_error_message(exc),
                    }
                )
        return templates.TemplateResponse(
            request,
            "index.html",
            context,
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/internal/jobs/recurring")
    def recurring_job_run(request: Request):
        try:
            _verify_internal_job_request(request)
        except HTTPException as exc:
            return JSONResponse(status_code=int(exc.status_code), content={"detail": str(exc.detail)})
        if not _db_enabled:
            return JSONResponse(status_code=503, content={"detail": "Database unavailable"})
        try:
            from fakturek.db import get_sessionmaker  # type: ignore

            SessionLocal = get_sessionmaker()
            with SessionLocal() as db:  # type: ignore
                result = _process_recurring_plans(db, request=request)
            return {
                "status": "ok",
                "created_invoice_ids": list(result.get("created_invoice_ids") or []),
                "sent_invoice_ids": list(result.get("sent_invoice_ids") or []),
                "errors": list(result.get("errors") or []),
            }
        except Exception:
            logging.getLogger("fakturek").exception("Recurring job failed")
            return JSONResponse(status_code=500, content={"detail": "Internal job failed"})

    @app.post("/internal/jobs/bank-sync")
    def bank_sync_job_run(request: Request):
        try:
            _verify_internal_job_request(request)
        except HTTPException as exc:
            return JSONResponse(status_code=int(exc.status_code), content={"detail": str(exc.detail)})
        if not _db_enabled:
            return JSONResponse(status_code=503, content={"detail": "Database unavailable"})
        try:
            from fakturek.db import get_sessionmaker  # type: ignore

            SessionLocal = get_sessionmaker()
            with SessionLocal() as db:  # type: ignore
                result = _process_bank_sync(db, request=request)
            return {"status": "ok", **result}
        except Exception:
            logging.getLogger("fakturek").exception("Bank sync job failed")
            return JSONResponse(status_code=500, content={"detail": "Internal job failed"})


    # --- Optional DB stack ----------------------------------------------
    #
    # The project uses SQLAlchemy + MySQL/MariaDB. Some environments (e.g. minimal
    # CI sandboxes) may not have the DB stack installed. To keep the app importable
    # and the basic healthcheck usable, we load DB-related dependencies lazily.
    try:  # pragma: no cover (covered in integration/DB tests, not unit tests)
        from sqlalchemy import case, func, or_, select
        from sqlalchemy.exc import SQLAlchemyError
        from sqlalchemy.orm import Session, selectinload

        from fakturek.db import db_ping, get_db, get_engine
        from fakturek.registry_sync import sync_contact_from_registry
        from fakturek.models import (
            ApiToken,
            ApiTokenMonthlyUsage,
            AuditLog,
            BankIncomingEmail,
            BankTransaction,
            Contact,
            Invoice,
            InvoiceCatalogItem,
            InvoiceEmail,
            InvoiceItem,
            InvoiceParty,
            InvoiceSeries,
            IssuerProfile,
            ImportRun,
            Payment,
            RecurringInvoicePlan,
            Subject,
            SubjectBankAccount,
            User,
            UserSubject,
        )

        # DB dependencies seem present; however the DB URL may require an extra
        # DBAPI driver (e.g. `pymysql`) that is not installed. Creating the engine
        # triggers dialect/driver imports but does NOT connect to the database.
        _db_enabled = True
        _db_import_error: str | None = None
        try:
            get_engine()
        except Exception as exc:  # pragma: no cover
            _db_enabled = False
            _db_import_error = str(exc)
    except ModuleNotFoundError as exc:  # pragma: no cover
        _db_enabled = False
        _db_import_error = str(exc)
        SQLAlchemyError = Exception  # type: ignore[assignment]
        Session = object  # type: ignore[assignment]



    def _parse_instance_bool(value: object | None, *, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        raw = str(value).strip().lower()
        if raw in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "disabled"}:
            return False
        return bool(default)

    def _format_instance_bool(value: bool) -> str:
        return "1" if bool(value) else "0"





    def _maybe_ensure_invoice_public_link(db: Session, *, invoice: Invoice, subject: Subject | None) -> None:
        if subject is None:
            return
        ensure_invoice_public_link(db, invoice=invoice, subject=subject)

    templates.env.globals["signup_enabled"] = bool(settings.signup_enabled and _db_enabled)

    if _db_enabled:
        from fakturek.api_v1 import create_api_v1_app

        app.mount("/api/v1", create_api_v1_app(settings=settings))

    # ------------------------------------------------------------------
    # RBAC enforcement middleware (phase-14)
    # ------------------------------------------------------------------
    #
    # Enforce per-subject RBAC.  By default, GET/HEAD requests require
    # "can_view" while state‑modifying requests (POST/PUT/PATCH/DELETE)
    # require "can_edit".  Future phases may introduce "can_issue" for
    # invoice issuance.  Middleware is added only when the DB stack is
    # enabled; otherwise RBAC is skipped entirely.
    if _db_enabled:
        from sqlalchemy import select  # type: ignore
        from fakturek.db import get_db  # type: ignore
        from fakturek.models import UserSubject  # type: ignore

        class RBACMiddleware(BaseHTTPMiddleware):
            def _authorization_unavailable_response(self) -> Response:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Authorization validation is temporarily unavailable"},
                    headers={"Retry-After": "5"},
                )

            def _access_denied_response(self, request: Request, *, required: str | None = None) -> Response:
                accept_header = (request.headers.get("accept") or "").lower()
                wants_json = "application/json" in accept_header and "text/html" not in accept_header
                if wants_json:
                    return JSONResponse(status_code=403, content={"detail": "Access denied"})

                labels = {
                    "can_view": "prohlížet tento subjekt",
                    "can_edit": "upravovat údaje tohoto subjektu",
                    "can_issue": "vystavovat doklady za tento subjekt",
                    "can_export": "exportovat data tohoto subjektu",
                }
                return templates.TemplateResponse(
                    request,
                    "access_denied.html",
                    {
                        "title": "Nemáš oprávnění",
                        "required_label": labels.get(required or "", "provést tuto akci"),
                        "required": required or "",
                        "current_path": _request_scope_path(request),
                    },
                    status_code=403,
                )

            async def dispatch(self, request: Request, call_next):  # type: ignore[override]
                # Skip RBAC when auth is disabled (dev) or for public endpoints.
                if not settings.auth_required:
                    return await call_next(request)
                path = _request_scope_path(request)
                # Skip static and auth/setup endpoints.
                if path.startswith("/static/") or path.startswith("/api/v1") or path in {
                    "/healthz",
                    "/healthz/db",
                    "/login",
                    "/logout",
                    "/password/reset",
                    "/setup",
                    "/signup",
                    "/signup/pending",
                    "/signup/verify",
                    "/signup/resend-verification",
                }:
                    return await call_next(request)

                if _is_public_invoice_path(path):
                    return await call_next(request)

                user_id = request.session.get("user_id")
                subject_id = request.session.get("subject_id")
                # If not authenticated yet, let Auth middleware handle redirect.
                if not user_id or not subject_id:
                    return await call_next(request)

                # Determine required permission based on HTTP verb + endpoint.
                if _path_requires_export_permission(path):
                    required = "can_export"
                elif path == "/invoices/new":
                    required = "can_issue"
                elif path == "/contacts/new" or (path.startswith("/contacts/") and path.endswith("/edit")):
                    required = "can_edit"
                elif path.startswith("/invoices/") and path.endswith("/edit"):
                    required = "can_edit"
                elif request.method in {"GET", "HEAD"}:
                    required = "can_view"
                else:
                    # Phase-18: invoice issuing requires can_issue (not just can_edit).
                    required = "can_issue" if path.endswith("/issue") or path == "/invoices/new" else "can_edit"

                # Acquire DB session manually (outside dependency system).
                db_gen = get_db()
                try:
                    db = next(db_gen)
                except Exception:
                    logging.getLogger("fakturek").exception(
                        "Authorization database session could not be opened"
                    )
                    return self._authorization_unavailable_response()
                try:
                    link = db.scalar(
                        select(UserSubject).where(
                            UserSubject.user_id == int(user_id),
                            UserSubject.subject_id == int(subject_id),
                        )
                    )
                except Exception:
                    logging.getLogger("fakturek").exception(
                        "Authorization validation failed"
                    )
                    return self._authorization_unavailable_response()
                finally:
                    # Properly close generator/session.
                    try:
                        next(db_gen)
                    except StopIteration:
                        pass

                # Missing link or insufficient permission -> deny.
                if not link:
                    return self._access_denied_response(request, required=required)
                if required == "can_view" and not bool(link.can_view):
                    return self._access_denied_response(request, required=required)
                if required == "can_edit" and not bool(link.can_edit):
                    return self._access_denied_response(request, required=required)
                if required == "can_issue" and not bool(getattr(link, "can_issue", False)):
                    return self._access_denied_response(request, required=required)
                if required == "can_export":
                    role_value = str(getattr(link, "role", "") or "").strip().lower()
                    if role_value != "owner" and not bool(getattr(link, "can_export", False)):
                        return self._access_denied_response(request, required=required)
                return await call_next(request)

        app.add_middleware(RBACMiddleware)







    class CSRFMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            try:
                await _verify_csrf(request)
            except HTTPException as exc:
                wants_html = "text/html" in str(request.headers.get("accept") or "")
                if wants_html:
                    return HTMLResponse(
                        content=(
                            '<!doctype html><html lang="cs"><head><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width, initial-scale=1">'
                            f'<title>{escape(str(exc.detail))}</title></head><body>'
                            f'<h1>{escape(str(exc.detail))}</h1>'
                            '<p>Obnov stránku a akci zkus znovu.</p>'
                            '</body></html>'
                        ),
                        status_code=int(exc.status_code),
                    )
                return JSONResponse(status_code=int(exc.status_code), content={"detail": str(exc.detail)})
            return await call_next(request)

    def _should_prevent_private_cache(request: Request) -> bool:
        if not settings.auth_required:
            return False
        path = _request_scope_path(request)
        if (
            path.startswith("/static/")
            or path.startswith("/internal/jobs/")
            or path.startswith("/api/v1")
            or path in {"/healthz", "/healthz/db"}
            or _is_public_invoice_path(path)
        ):
            return False
        return True

    def _apply_security_headers(request: Request, response: Response) -> Response:
        headers = response.headers
        if _should_prevent_private_cache(request):
            headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"
        if "x-content-type-options" not in headers:
            headers["X-Content-Type-Options"] = "nosniff"
        if "x-frame-options" not in headers:
            headers["X-Frame-Options"] = "DENY"
        if settings.app_env == "prod" and "strict-transport-security" not in headers:
            headers["Strict-Transport-Security"] = "max-age=63072000"
        if "referrer-policy" not in headers:
            headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if "permissions-policy" not in headers:
            headers["Permissions-Policy"] = "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
        content_type = str(headers.get("content-type") or "").lower()
        if "text/html" in content_type and "content-security-policy" not in headers:
            csp_parts = [
                "default-src 'self'",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
                "img-src 'self' data: blob:",
                "object-src 'none'",
                "script-src 'self' 'unsafe-inline'",
                "style-src 'self' 'unsafe-inline'",
                "font-src 'self' data:",
                "connect-src 'self'",
                "frame-src 'none'",
            ]
            if str(request.scope.get("scheme") or "").lower() == "https":
                csp_parts.append("upgrade-insecure-requests")
            headers["Content-Security-Policy"] = "; ".join(csp_parts)
        return response

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            return _apply_security_headers(request, await call_next(request))

    class RequestBodyLimitMiddleware:
        def __init__(self, app, max_body_bytes: int):
            self.app = app
            self.max_body_bytes = int(max_body_bytes)

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                return await self.app(scope, receive, send)
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            raw_length = headers.get(b"content-length")
            if raw_length:
                try:
                    if int(raw_length) > self.max_body_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content={"detail": "Request body too large"},
                        )
                        return await response(scope, receive, send)
                except ValueError:
                    response = JSONResponse(
                        status_code=400,
                        content={"detail": "Invalid Content-Length"},
                    )
                    return await response(scope, receive, send)

            consumed = 0

            async def limited_receive():
                nonlocal consumed
                message = await receive()
                if message.get("type") == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > self.max_body_bytes:
                        raise HTTPException(status_code=413, detail="Request body too large")
                return message

            try:
                return await self.app(scope, limited_receive, send)
            except HTTPException as exc:
                response = JSONResponse(
                    status_code=int(exc.status_code),
                    content={"detail": str(exc.detail)},
                )
                return await response(scope, receive, send)

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=max(
            2 * 1024 * 1024,
            int(settings.import_max_upload_mb) * 1024 * 1024 + 1024 * 1024,
        ),
    )
    def _should_translate_ui_html(request: Request) -> bool:
        path = _request_scope_path(request)
        if path.startswith("/static/") or path.startswith("/api/") or path.startswith("/healthz"):
            return False
        # Public/print invoice documents use the separate per-invoice language
        # field. The UI language must not silently rewrite legal documents.
        if _is_public_invoice_path(path):
            return False
        if path.endswith("/print") or "/print/" in path:
            return False
        return True

    class UiLanguageMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            language = _normalize_ui_language(request.session.get("ui_language"))
            response = await call_next(request)
            if language != "en" or not _should_translate_ui_html(request):
                return response
            content_type = str(response.headers.get("content-type") or "").lower()
            if "text/html" not in content_type:
                return response
            body = b""
            try:
                async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                    body += chunk
                charset = "utf-8"
                match = re.search(r"charset=([^;]+)", content_type)
                if match:
                    charset = match.group(1).strip() or "utf-8"
                html = body.decode(charset, errors="replace")
                translated = translate_html_document(html, language)
                headers = dict(response.headers)
                headers.pop("content-length", None)
                return Response(
                    content=translated.encode(charset),
                    status_code=response.status_code,
                    headers=headers,
                    media_type=None,
                    background=getattr(response, "background", None),
                )
            except Exception:
                logging.getLogger("fakturek").exception("Failed to translate UI HTML")
                if body:
                    headers = dict(response.headers)
                    headers.pop("content-length", None)
                    return Response(
                        content=body,
                        status_code=response.status_code,
                        headers=headers,
                        media_type=None,
                        background=getattr(response, "background", None),
                    )
                return response

    # Must be inside SessionMiddleware (added below) so request.session exists,
    # but outside route handlers so it covers the whole logged-in UI.
    app.add_middleware(UiLanguageMiddleware)

    # ------------------------------------------------------------------
    # Session cookies (phase-13)
    # ------------------------------------------------------------------
    # IMPORTANT: SessionMiddleware must wrap (run before) any middleware that
    # accesses request.session (AuthRequired/RBAC). In Starlette/FastAPI, the
    # LAST added middleware runs first, so we add SessionMiddleware last.
    session_cookie_domain = None
    session_cookie_name = (os.getenv("SESSION_COOKIE_NAME") or "").strip()
    session_https_only = bool(settings.app_env == "prod")
    try:
        public_host = (urlsplit(str(getattr(settings, "public_base_url", "") or "")).hostname or "").lower()
        app_url = urlsplit(str(getattr(settings, "app_base_url", "") or ""))
        app_host = (app_url.hostname or "").lower()
        if str(app_url.scheme or "").lower() == "https":
            session_https_only = True
        if settings.app_env == "prod" and public_host == "fakturek.cz" and app_host.endswith(".fakturek.cz"):
            session_cookie_domain = ".fakturek.cz"
        elif not session_cookie_name and app_host and app_host != "app.fakturek.cz":
            # Keep staging/dev subdomains isolated from a production
            # .fakturek.cz cookie with the same name. Safari otherwise may send
            # both cookies and login/logout can look stuck.
            session_cookie_name = f"fakturek_{re.sub(r'[^a-z0-9]+', '_', app_host).strip('_')[:32]}_session"
    except Exception:
        session_cookie_domain = None
    if not session_cookie_name:
        session_cookie_name = "fakturek_session"

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=session_cookie_name,
        https_only=session_https_only,
        same_site="lax",
        max_age=60 * 60 * 24 * 14,
        domain=session_cookie_domain,
    )
    if settings.app_env == "prod":
        allowed_hosts = sorted(
            {
                urlsplit(settings.public_base_url).hostname,
                urlsplit(settings.app_base_url).hostname,
                "127.0.0.1",
                "localhost",
            }
            - {None}
        )
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=allowed_hosts,
            www_redirect=False,
        )
    # Security headers must wrap TrustedHostMiddleware so even rejected Host
    # headers receive the same browser protections as regular responses.
    app.add_middleware(SecurityHeadersMiddleware)

    # Must be OUTERMOST so it can catch exceptions from everything below.
    # In Starlette/FastAPI, the last added middleware runs first.
    app.add_middleware(_VerboseErrorMiddleware)

    @app.get("/healthz/db")
    def healthz_db():
        if not _db_enabled:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "detail": _safe_db_error_message(),
                },
            )

        try:
            db_ping()  # type: ignore[misc]
        except SQLAlchemyError as exc:  # type: ignore[misc]
            return JSONResponse(status_code=503, content={"status": "error", "detail": _safe_db_error_message(exc)})
        except Exception as exc:  # pragma: no cover
            return JSONResponse(status_code=503, content={"status": "error", "detail": _safe_db_error_message(exc)})

        return {"status": "ok"}

    def _render_action_confirm(
        request: Request,
        *,
        title: str,
        message: str,
        action_url: str,
        submit_label: str,
        hidden_fields: dict[str, str] | None = None,
        cancel_url: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "auth/confirm.html",
            {
                "title": title,
                "message": message,
                "action_url": action_url,
                "submit_label": submit_label,
                "hidden_fields": hidden_fields or {},
                "cancel_url": cancel_url or "/",
            },
            status_code=status_code,
        )

    # ------------------------------------------------------------------
    # Auth (phase-13): login/logout + session cookies
    # ------------------------------------------------------------------

    SIGNUP_VERIFICATION_TOKEN_SALT = "signup-email-verification-v1"
    SIGNUP_VERIFICATION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
    PASSWORD_RESET_TOKEN_SALT = "password-reset-v1"
    PASSWORD_RESET_MAX_AGE_SECONDS = 60 * 60

    def _signup_verification_serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(str(settings.signup_token_key or ""))

    def _password_reset_serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(str(settings.signup_token_key or ""))

    def _build_signup_verification_token(user: User, *, next_url: str | None = None) -> str:
        safe_next_url = _safe_next_url(next_url, _signup_created_next_default())
        return _signup_verification_serializer().dumps(
            {
                "purpose": "signup_email_verification",
                "user_id": int(user.id),
                "email": str(getattr(user, "email", "") or "").strip().lower(),
                "next_url": safe_next_url,
            },
            salt=SIGNUP_VERIFICATION_TOKEN_SALT,
        )

    def _resolve_external_base_url(request: Request) -> str:
        public_base = resolve_public_base_url(
            request=request,
            configured_base_url=getattr(settings, "public_base_url", "") or None,
            trusted_proxy_ips=getattr(settings, "trusted_proxy_ips", ()),
        )
        if public_base:
            return str(public_base).rstrip("/")
        try:
            return str(request.base_url).rstrip("/")
        except Exception:
            return ""

    def _resolve_app_base_url(request: Request) -> str:
        configured_app_base = str(getattr(settings, "app_base_url", "") or "").strip().rstrip("/")
        if configured_app_base:
            return configured_app_base
        return _resolve_external_base_url(request)

    def _build_signup_verification_url(request: Request, user: User, *, next_url: str | None = None) -> str:
        base_url = _resolve_app_base_url(request)
        token = _build_signup_verification_token(user, next_url=next_url)
        return f"{base_url}/signup/verify?token={quote(token)}"

    def _build_signup_verification_email(
        *,
        request: Request,
        user: User,
        next_url: str | None = None,
    ) -> tuple[SMTPConfig, str, str, str]:
        verify_url = _build_signup_verification_url(request, user, next_url=next_url)
        from_email = (settings.smtp_from_email or settings.issuer_email or "").strip()
        from_name = (settings.smtp_from_name or settings.issuer_name or "Fakturek.cz").strip()
        smtp_cfg = SMTPConfig(
            host=settings.smtp_host,
            port=int(settings.smtp_port or 0),
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=bool(settings.smtp_use_tls),
            use_starttls=bool(settings.smtp_use_starttls),
            timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
            from_email=from_email,
            from_name=from_name,
        )
        body = "\n".join(
            [
                "Dobrý den,",
                "",
                "děkujeme za registraci do Fakturku.",
                "Pro dokončení registrace a aktivaci účtu klikni na tento odkaz:",
                verify_url,
                "",
                "Odkaz platí 7 dní.",
                "Pokud jsi registraci nezadával(a), tento e-mail ignoruj.",
                "",
                "Fakturek.cz",
            ]
        )
        return smtp_cfg, from_email, from_name, body

    def _send_signup_verification_email(
        db: Session,
        *,
        request: Request,
        user: User,
        next_url: str | None = None,
    ) -> tuple[bool, str | None]:
        safe_next_url = _safe_next_url(next_url, _signup_created_next_default())
        smtp_cfg, from_email, from_name, body = _build_signup_verification_email(request=request, user=user, next_url=safe_next_url)
        if not smtp_is_configured(smtp_cfg):
            return False, "SMTP není nastavené. Potvrzovací e-mail teď nejde odeslat."
        if not looks_like_email(from_email):
            return False, "Chybí odesílatel (From) pro potvrzovací e-mail."
        try:
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=[str(getattr(user, "email", "") or "").strip()],
                subject="Potvrzení registrace do Fakturku",
                body=body,
            )
            message_id, _debug = send_via_smtp(smtp_cfg, msg)
            _audit_log(
                db,
                request=request,
                action="signup_verification_email_sent",
                entity_type="user",
                entity_id=int(user.id),
                user_id=int(user.id),
                data={
                    "to_email": str(getattr(user, "email", "") or "").strip(),
                    "message_id": message_id,
                    "next_url": safe_next_url,
                },
                subject_id=None,
            )
            db.commit()
            return True, None
        except Exception as exc:
            db.rollback()
            logging.getLogger("fakturek").error(
                "Failed to send signup verification email (error_type=%s)",
                type(exc).__name__,
            )
            return False, "Potvrzovací e-mail se teď nepodařilo odeslat."

    def _load_signup_verification_payload(token: str) -> dict[str, object]:
        return _signup_verification_serializer().loads(
            str(token or ""),
            salt=SIGNUP_VERIFICATION_TOKEN_SALT,
            max_age=SIGNUP_VERIFICATION_MAX_AGE_SECONDS,
        )

    def _build_password_reset_token(user: User) -> str:
        return _password_reset_serializer().dumps(
            {
                "purpose": "password_reset",
                "user_id": int(user.id),
                "email": str(getattr(user, "email", "") or "").strip().lower(),
                "session_version": int(getattr(user, "session_version", 1) or 1),
            },
            salt=PASSWORD_RESET_TOKEN_SALT,
        )

    def _build_password_reset_url(request: Request, user: User) -> str:
        base_url = _resolve_app_base_url(request)
        token = _build_password_reset_token(user)
        return f"{base_url}/password/reset?token={quote(token)}"

    def _load_password_reset_payload(token: str) -> dict[str, object]:
        return _password_reset_serializer().loads(
            str(token or ""),
            salt=PASSWORD_RESET_TOKEN_SALT,
            max_age=PASSWORD_RESET_MAX_AGE_SECONDS,
        )

    def _password_reset_smtp_config() -> tuple[SMTPConfig, str, str]:
        from_email = (settings.smtp_from_email or settings.issuer_email or "").strip()
        from_name = (settings.smtp_from_name or settings.issuer_name or "Fakturek.cz").strip()
        smtp_cfg = SMTPConfig(
            host=settings.smtp_host,
            port=int(settings.smtp_port or 0),
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=bool(settings.smtp_use_tls),
            use_starttls=bool(settings.smtp_use_starttls),
            timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
            from_email=from_email,
            from_name=from_name,
        )
        return smtp_cfg, from_email, from_name

    def _send_password_reset_email(db: Session, *, request: Request, user: User) -> tuple[bool, str | None]:
        smtp_cfg, from_email, from_name = _password_reset_smtp_config()
        if not smtp_is_configured(smtp_cfg):
            return False, "SMTP není nastavené. Reset hesla teď nejde odeslat."
        if not looks_like_email(from_email):
            return False, "Chybí odesílatel (From) pro reset hesla."

        reset_url = _build_password_reset_url(request, user)
        body = "\n".join(
            [
                "Dobrý den,",
                "",
                "požádal(a) jsi o nastavení nového hesla do Fakturku.",
                "Pokračuj přes tento odkaz:",
                reset_url,
                "",
                "Odkaz platí 1 hodinu a po změně hesla přestane fungovat.",
                "Pokud jsi o reset nežádal(a), tenhle e-mail ignoruj.",
                "",
                "Fakturek.cz",
            ]
        )
        try:
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=[str(getattr(user, "email", "") or "").strip()],
                subject="Reset hesla do Fakturku",
                body=body,
            )
            message_id, _debug = send_via_smtp(smtp_cfg, msg)
            _audit_log(
                db,
                request=request,
                action="password_reset_email_sent",
                entity_type="user",
                entity_id=int(user.id),
                user_id=int(user.id),
                subject_id=None,
                data={
                    "to_email": str(getattr(user, "email", "") or "").strip(),
                    "message_id": message_id,
                },
            )
            db.commit()
            return True, None
        except Exception as exc:
            db.rollback()
            logging.getLogger("fakturek").error(
                "Failed to send password reset email (error_type=%s)",
                type(exc).__name__,
            )
            return False, "E-mail pro reset hesla se teď nepodařilo odeslat."

    def _mask_email(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw or "@" not in raw:
            return ""
        local, _, domain = raw.partition("@")
        if len(local) <= 2:
            masked_local = local[:1] + "•"
        else:
            masked_local = local[:2] + "•" * max(1, len(local) - 2)
        return f"{masked_local}@{domain}"

    def _login_page_context(
        *,
        request: Request,
        next_url: str,
        prefill: dict[str, str] | None = None,
        error: str | None = None,
        info: str | None = None,
        status_code: int = 200,
        verification_pending_email: str | None = None,
    ) -> HTMLResponse:
        if not _db_enabled:
            return templates.TemplateResponse(
                request,
                "auth/login.html",
                {
                    "db_enabled": False,
                    "db_error": _safe_db_error_message(),
                    "next_url": next_url,
                    "prefill": prefill or {"identifier": ""},
                    "error": error,
                    "info": info,
                    "setup_available": False,
                    "setup_requires_token": True,
                    "verification_pending_email": verification_pending_email,
                },
                status_code=status_code,
            )

        users_count = 0
        db_error: str | None = None
        try:
            from sqlalchemy import func, select  # type: ignore
            from fakturek.db import get_sessionmaker  # type: ignore

            SessionLocal = get_sessionmaker()
            with SessionLocal() as db:  # type: ignore
                users_count = int(db.scalar(select(func.count(User.id))) or 0)
        except Exception as exc:
            db_error = _safe_db_error_message(exc)

        setup_requires_token = True
        setup_available = (db_error is None) and users_count == 0 and bool(settings.setup_token)

        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "db_enabled": db_error is None,
                "db_error": db_error,
                "next_url": next_url,
                "prefill": prefill or {"identifier": ""},
                "error": error,
                "info": info,
                "setup_available": setup_available,
                "setup_requires_token": setup_requires_token,
                "verification_pending_email": verification_pending_email,
            },
            status_code=status_code,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(
        request: Request,
        next: str | None = None,
        verified: bool = False,
        verification_sent: bool = False,
        resent: bool = False,
        expired: bool = False,
        paid_signup: bool = False,
        identifier: str | None = None,
        reset: bool = False,
    ):
        next_url = _safe_next_url(next, "/")

        # Already logged in.
        if request.session.get("user_id"):
            return RedirectResponse(url=next_url, status_code=303)
        info = None
        if reset:
            info = "Heslo je změněné. Přihlas se prosím novým heslem."
        elif paid_signup:
            info = "Platba proběhla v pořádku. Účet je aktivní, přihlas se prosím a můžeš pokračovat ve Fakturku."
        elif verified:
            info = "E-mail je potvrzený. Účet je aktivní a můžeš se přihlásit."
        elif verification_sent:
            info = "Účet je založený. Teď už jen potvrď registraci přes odkaz v e-mailu."
        elif resent:
            info = "Poslal jsem nový potvrzovací e-mail."
        elif expired:
            info = "Přihlášení vypršelo. Přihlas se prosím znovu."
        return _login_page_context(
            request=request,
            next_url=next_url,
            prefill={"identifier": str(identifier or "").strip()},
            info=info,
        )

    @app.get("/password/reset", response_class=HTMLResponse)
    def password_reset_page(request: Request, token: str | None = None):
        if request.session.get("user_id"):
            return RedirectResponse(url="/settings#security", status_code=303)
        token_value = str(token or "").strip()
        return templates.TemplateResponse(
            request,
            "auth/password_reset.html",
            {
                "mode": "reset" if token_value else "request",
                "token": token_value,
                "prefill": {"email": ""},
                "error": None,
                "info": None,
            },
        )

    @app.post("/password/reset", response_class=HTMLResponse)
    async def password_reset_submit(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        mode = str(form.get("mode") or "request").strip()

        if mode == "reset":
            token = str(form.get("token") or "").strip()
            new_password = str(form.get("new_password") or "")
            new_password2 = str(form.get("new_password2") or "")

            def _render_reset_error(message: str, *, status_code: int = 400) -> HTMLResponse:
                return templates.TemplateResponse(
                    request,
                    "auth/password_reset.html",
                    {
                        "mode": "reset",
                        "token": token,
                        "prefill": {"email": ""},
                        "error": message,
                        "info": None,
                    },
                    status_code=status_code,
                )

            if not token:
                return _render_reset_error("Odkaz pro reset hesla je neplatný.", status_code=400)
            password_error = new_password_length_error(new_password)
            if password_error:
                return _render_reset_error(password_error, status_code=400)
            if new_password != new_password2:
                return _render_reset_error("Nová hesla se neshodují.", status_code=400)

            try:
                payload = _load_password_reset_payload(token)
            except SignatureExpired:
                return _render_reset_error("Odkaz pro reset hesla vypršel. Požádej si prosím o nový.", status_code=400)
            except BadSignature:
                return _render_reset_error("Odkaz pro reset hesla je neplatný.", status_code=400)

            if str(payload.get("purpose") or "") != "password_reset":
                return _render_reset_error("Odkaz pro reset hesla je neplatný.", status_code=400)
            user_id = int(payload.get("user_id") or 0)
            email = str(payload.get("email") or "").strip().lower()
            token_session_version = int(payload.get("session_version") or 0)
            user = db.get(User, user_id) if user_id > 0 else None
            if (
                user is None
                or not bool(getattr(user, "is_active", False))
                or str(getattr(user, "email", "") or "").strip().lower() != email
                or int(getattr(user, "session_version", 1) or 1) != token_session_version
            ):
                return _render_reset_error("Odkaz pro reset hesla už není platný. Požádej si prosím o nový.", status_code=400)

            try:
                user.password_hash = hash_password(new_password)
                user.failed_login_count = 0
                user.failed_login_locked_until = None
                user.session_version = int(getattr(user, "session_version", 1) or 1) + 1
                db.add(user)
                _audit_log(
                    db,
                    request=request,
                    action="password_reset_completed",
                    entity_type="user",
                    entity_id=int(user.id),
                    user_id=int(user.id),
                    subject_id=None,
                    data={"email": email},
                )
                db.commit()
            except SQLAlchemyError:  # type: ignore[misc]
                db.rollback()
                logging.getLogger("fakturek").exception("Failed to persist password reset")
                return _render_reset_error("Heslo se teď nepodařilo uložit. Zkus to prosím později.", status_code=500)

            return RedirectResponse(url="/login?reset=1", status_code=303)

        _auth_email_rate_limit_or_429(request)
        email = str(form.get("email") or "").strip().lower()
        info = "Pokud u nás tenhle e-mail existuje, poslali jsme na něj odkaz pro nastavení nového hesla."

        def _render_request(*, error: str | None = None, status_code: int = 200) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "auth/password_reset.html",
                {
                    "mode": "request",
                    "token": "",
                    "prefill": {"email": email},
                    "error": error,
                    "info": None if error else info,
                },
                status_code=status_code,
            )

        if not looks_like_email(email):
            return _render_request(error="Zadej platný e-mail.", status_code=400)

        try:
            user = db.scalar(select(User).where(func.lower(User.email) == email).limit(1))
        except SQLAlchemyError:
            return _render_request(error="Databáze teď není dostupná. Zkus to prosím později.", status_code=503)

        if user is None or not bool(getattr(user, "is_active", False)):
            return _render_request()

        sent, error = _send_password_reset_email(db, request=request, user=user)
        if not sent:
            logging.getLogger("fakturek").warning("Password reset email was not sent: %s", error or "unknown error")
        return _render_request()

    @app.get("/logout", response_class=HTMLResponse)
    def logout_confirm(request: Request, next: str | None = None):
        safe_next = _safe_next_url(next, "/")
        if not request.session.get("user_id"):
            return RedirectResponse(url=safe_next, status_code=303)
        return _render_action_confirm(
            request,
            title="Odhlášení",
            message="Opravdu se chceš odhlásit z aktuální relace?",
            action_url="/logout",
            submit_label="Odhlásit",
            hidden_fields={"next": safe_next},
            cancel_url=safe_next,
        )

    @app.post("/logout")
    async def logout_submit(request: Request):
        await request.form()
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # ------------------------------------------------------------------
    # Subject switching (phase-14)
    # ------------------------------------------------------------------
    #
    # Logged-in users can switch between billing entities (subjects) that
    # they have access to.  The target subject id must correspond to a
    # row in ``user_subjects`` with ``can_view`` set to True.  Switching
    # changes the ``subject_id`` stored in the session.  Unauthorized
    # attempts result in a 403.
    if _db_enabled:

        def _load_subject_switch_link(db: Session, *, user_id: int, subject_id: int):
            from sqlalchemy import select  # type: ignore
            from sqlalchemy.exc import SQLAlchemyError  # type: ignore
            from fakturek.models import UserSubject  # type: ignore

            try:
                return db.scalar(
                    select(UserSubject).where(
                        UserSubject.user_id == int(user_id),
                        UserSubject.subject_id == int(subject_id),
                    )
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                raise HTTPException(status_code=503, detail=_safe_db_error_message(exc)) from exc

        def _verify_subject_switch_token(request: Request, *, subject_id: int, next_url: str, st: str | None) -> None:
            if not settings.auth_required:
                return
            expected_switch_token = _subject_switch_token_for_request(
                request,
                subject_id=int(subject_id),
                next_url=next_url,
            )
            if not st or not secrets.compare_digest(str(st), expected_switch_token):
                raise HTTPException(status_code=403, detail="Invalid subject switch token")

        @app.get("/subjects/{subject_id}/switch", response_class=HTMLResponse)
        def subject_switch_confirm(
            request: Request,
            subject_id: int,
            next: str | None = None,
            st: str | None = None,
            db: Session = Depends(get_db),
        ):
            user_id = request.session.get("user_id")
            if user_id is None:
                target = _safe_next_url(next, "/")
                return RedirectResponse(url=f"/login?next={quote(target)}", status_code=303)

            safe_next = _safe_next_url(next, "/")
            _verify_subject_switch_token(request, subject_id=int(subject_id), next_url=safe_next, st=st)
            link = _load_subject_switch_link(db, user_id=int(user_id), subject_id=int(subject_id))
            if link is None or not bool(link.can_view):
                raise HTTPException(status_code=403, detail="Access denied")

            subject = db.get(Subject, int(subject_id))
            subject_name = str(getattr(subject, "name", "") or f"Subjekt {int(subject_id)}")
            return _render_action_confirm(
                request,
                title="Přepnutí subjektu",
                message=f"Přepnout aktivní organizaci na „{subject_name}“?",
                action_url=f"/subjects/{int(subject_id)}/switch",
                submit_label="Přepnout",
                hidden_fields=_subject_switch_form_values(request, subject_id=int(subject_id), next_url=safe_next),
                cancel_url=safe_next,
            )

        @app.post("/subjects/{subject_id}/switch")
        async def subject_switch_submit(
            request: Request,
            subject_id: int,
            db: Session = Depends(get_db),
        ):
            user_id = request.session.get("user_id")
            form = await _request_form_once(request)
            safe_next = _safe_next_url(form.get("next"), "/")
            if user_id is None:
                return RedirectResponse(url=f"/login?next={quote(safe_next)}", status_code=303)

            _verify_subject_switch_token(
                request,
                subject_id=int(subject_id),
                next_url=safe_next,
                st=str(form.get("st") or "") or None,
            )
            link = _load_subject_switch_link(db, user_id=int(user_id), subject_id=int(subject_id))
            if link is None or not bool(link.can_view):
                raise HTTPException(status_code=403, detail="Access denied")

            request.session["subject_id"] = int(subject_id)
            return RedirectResponse(url=safe_next, status_code=303)

    if _db_enabled:

        @app.post("/login")
        async def login_submit(request: Request, db: Session = Depends(get_db)):
            # Phase-29: apply login rate limiting and CSRF protection.
            _login_rate_limit_or_429(request)
            await _verify_csrf(request)

            form = await request.form()
            identifier = (form.get("identifier") or "").strip()
            password = str(form.get("password") or "")
            next_url = _safe_next_url(form.get("next"), "/")

            prefill = {"identifier": identifier}

            if not identifier or not password:
                return _login_page_context(
                    request=request,
                    next_url=next_url,
                    prefill=prefill,
                    error="Vyplň uživatele/e-mail a heslo.",
                    status_code=400,
                )

            try:
                user = db.scalar(
                    select(User).where(or_(User.username == identifier, User.email == identifier))
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _login_page_context(
                    request=request,
                    next_url=next_url,
                    prefill=prefill,
                    error="Databáze není dostupná – nelze se přihlásit.",
                    status_code=503,
                )

            password_valid = verify_password(password, str(user.password_hash or "") if user is not None else "")
            if user is None or not password_valid:
                return _login_page_context(
                    request=request,
                    next_url=next_url,
                    prefill=prefill,
                    error="Neplatné přihlašovací údaje.",
                    status_code=401,
                )

            if not bool(user.is_active):
                verification_pending = getattr(user, "email_verified_at", None) is None
                deletion_pending = getattr(user, "deletion_requested_at", None) is not None
                return _login_page_context(
                    request=request,
                    next_url=next_url,
                    prefill=prefill,
                    error=(
                        "Účet ještě není potvrzený. Otevři potvrzovací odkaz z e-mailu."
                        if verification_pending
                        else "Účet je zrušený a čeká na bezpečné smazání."
                        if deletion_pending
                        else "Účet je deaktivovaný."
                    ),
                    verification_pending_email=(str(getattr(user, "email", "") or "").strip() if verification_pending else None),
                    status_code=403,
                )

            # Session: keep only minimal public claims.
            now_utc = utc_now()
            request.session.clear()
            request.session["user_id"] = int(user.id)
            request.session["username"] = str(user.username)
            request.session["ui_theme"] = _normalize_ui_theme(getattr(user, "ui_theme", "system"))
            request.session["ui_language"] = _normalize_ui_language(getattr(user, "ui_language", "cs"))
            request.session["authenticated_at"] = now_utc.isoformat()
            request.session["session_version"] = int(getattr(user, "session_version", 1) or 1)
            # Determine default subject for this user.  Choose the first
            # linked subject (ordered by subject_id) when available,
            # otherwise fall back to the seeded default (1).
            try:
                from fakturek.models import UserSubject  # type: ignore

                link = db.scalar(
                    select(UserSubject)
                    .where(UserSubject.user_id == int(user.id))
                    .where(UserSubject.can_view.is_(True))
                    .order_by(UserSubject.subject_id.asc())
                )
                if link is None:
                    link = db.scalar(
                        select(UserSubject)
                        .where(UserSubject.user_id == int(user.id))
                        .order_by(UserSubject.subject_id.asc())
                    )
                if link is not None:
                    request.session["subject_id"] = int(link.subject_id)
                else:
                    request.session["subject_id"] = DEFAULT_SUBJECT_ID  # type: ignore[name-defined]
            except Exception:
                # If lookup fails, fall back to default subject id.
                request.session["subject_id"] = DEFAULT_SUBJECT_ID  # type: ignore[name-defined]

            # Best-effort upgrades / metadata.
            try:
                if needs_rehash(user.password_hash):
                    user.password_hash = hash_password(password)
                user.last_login_at = utc_now()
                user.failed_login_count = 0
                user.failed_login_locked_until = None
                db.commit()
            except SQLAlchemyError:
                db.rollback()

            return RedirectResponse(url=next_url, status_code=303)

        def _signup_available() -> bool:
            return bool(settings.signup_enabled and _db_enabled)

        SUPPORTED_SIGNUP_SUBJECT_COUNTRY = "CZ"

        def _signup_created_next_default() -> str:
            return "/"

        def _is_supported_signup_subject_country(value: object | None) -> bool:
            return str(value or "").strip().upper() == SUPPORTED_SIGNUP_SUBJECT_COUNTRY

        def _signup_prefill_from_values(values) -> dict[str, str]:
            email = str(values.get("email") or "").strip().lower()
            subject_email = str(values.get("subject_email") or "").strip().lower()
            return {
                "username": str(values.get("username") or "").strip(),
                "email": email,
                "subject_name": str(values.get("subject_name") or "").strip(),
                "subject_email": subject_email or email,
                "subject_phone": str(values.get("subject_phone") or "").strip(),
                "subject_street": str(values.get("subject_street") or "").strip(),
                "subject_city": str(values.get("subject_city") or "").strip(),
                "subject_zip": str(values.get("subject_zip") or "").strip(),
                "subject_ico": str(values.get("subject_ico") or "").strip(),
                "subject_dic": str(values.get("subject_dic") or "").strip(),
                "subject_bank_account": str(values.get("subject_bank_account") or "").strip(),
                "subject_country": (str(values.get("subject_country") or "CZ").strip().upper() or "CZ"),
                "subject_default_currency": (str(values.get("subject_default_currency") or "CZK").strip().upper() or "CZK"),
            }

        def _blank_signup_prefill() -> dict[str, str]:
            return _signup_prefill_from_values({})

        def _render_signup_page(
            request: Request,
            *,
            next_url: str,
            prefill: dict[str, str] | None = None,
            error: str | None = None,
            info: str | None = None,
            status_code: int = 200,
        ) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "auth/signup.html",
                {
                    "db_enabled": _db_enabled,
                    "signup_enabled": _signup_available(),
                    "next_url": _safe_next_url(next_url, _signup_created_next_default()),
                    "prefill": prefill or _blank_signup_prefill(),
                    "error": error,
                    "info": info,
                    "country_options": [("CZ", "CZ – Česká republika")],
                    "currency_options": _build_currency_options((prefill or {}).get("subject_default_currency") or "CZK"),
                },
                status_code=status_code,
            )

        def _render_signup_pending_page(
            request: Request,
            *,
            email: str | None,
            email_sent: bool,
            next_url: str,
            error: str | None = None,
            info: str | None = None,
            status_code: int = 200,
        ) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "auth/signup_pending.html",
                {
                    "email": str(email or "").strip(),
                    "masked_email": _mask_email(email),
                    "email_sent": bool(email_sent),
                    "next_url": _safe_next_url(next_url, _signup_created_next_default()),
                    "error": error,
                    "info": info,
                },
                status_code=status_code,
            )



        @app.get("/signup", response_class=HTMLResponse)
        def signup_page(request: Request, next: str | None = None):
            if request.session.get("user_id"):
                return RedirectResponse(url=_safe_next_url(next, "/"), status_code=303)
            if not _signup_available():
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            return _render_signup_page(
                request, next_url=_safe_next_url(next, _signup_created_next_default())
            )

        @app.get("/signup/pending", response_class=HTMLResponse)
        def signup_pending_page(
            request: Request,
            sent: bool = False,
            next: str | None = None,
        ):
            if request.session.get("user_id"):
                return RedirectResponse(url=_safe_next_url(next, "/"), status_code=303)
            return _render_signup_pending_page(
                request,
                email=str(request.session.get("signup_pending_email") or ""),
                email_sent=bool(sent),
                next_url=_safe_next_url(next, _signup_created_next_default()),
            )

        @app.post("/signup/resend-verification")
        async def signup_resend_verification(request: Request, db: Session = Depends(get_db)):
            if not _signup_available():
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            await _verify_csrf(request)
            _auth_email_rate_limit_or_429(request)
            form = await _request_form_once(request)
            email = str(form.get("email") or "").strip().lower()
            next_url = _safe_next_url(form.get("next"), "/login")

            if not email or not looks_like_email(email):
                return _render_signup_pending_page(
                    request,
                    email=email,
                    email_sent=False,
                    next_url=next_url,
                    error="Zadej platný e-mail, na který máme poslat nový potvrzovací odkaz.",
                    status_code=400,
                )

            user = db.scalar(select(User).where(func.lower(User.email) == email).limit(1))
            if user is None:
                return _render_signup_pending_page(
                    request,
                    email=email,
                    email_sent=False,
                    next_url=next_url,
                    info="Jestli ten účet existuje, poslal jsem nový potvrzovací e-mail.",
                )
            if getattr(user, "email_verified_at", None) is not None and bool(getattr(user, "is_active", False)):
                return _render_signup_pending_page(
                    request,
                    email=email,
                    email_sent=False,
                    next_url=next_url,
                    info="Jestli ten účet existuje, poslal jsem nový potvrzovací e-mail.",
                )

            sent_ok, send_error = _send_signup_verification_email(db, request=request, user=user, next_url=next_url)
            if not sent_ok:
                logging.getLogger("fakturek").warning(
                    "Signup verification email was not sent: %s", send_error or "unknown error"
                )
            return _render_signup_pending_page(
                request,
                email=email,
                email_sent=False,
                next_url=next_url,
                info="Jestli ten účet existuje, poslal jsem nový potvrzovací e-mail.",
            )

        @app.get("/signup/verify")
        def signup_verify(request: Request, token: str | None = None, db: Session = Depends(get_db)):
            if not token:
                return RedirectResponse(url="/login", status_code=303)
            try:
                payload = _load_signup_verification_payload(token)
            except SignatureExpired:
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Potvrzovací odkaz už vypršel. Pošli si nový.",
                    status_code=400,
                )
            except BadSignature:
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Potvrzovací odkaz není platný.",
                    status_code=400,
                )

            if str(payload.get("purpose") or "") != "signup_email_verification":
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Potvrzovací odkaz není platný.",
                    status_code=400,
                )

            try:
                user_id = int(payload.get("user_id") or 0)
            except Exception:
                user_id = 0
            email = str(payload.get("email") or "").strip().lower()
            if user_id <= 0 or not email:
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Potvrzovací odkaz není platný.",
                    status_code=400,
                )

            user = db.get(User, user_id)
            if user is None or str(getattr(user, "email", "") or "").strip().lower() != email:
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Potvrzovaný účet už neexistuje.",
                    status_code=404,
                )

            if getattr(user, "email_verified_at", None) is not None:
                if not bool(getattr(user, "is_active", False)):
                    return _login_page_context(
                        request=request,
                        next_url="/login",
                        error="Účet je deaktivovaný. Aktivační odkaz ho nemůže znovu zapnout.",
                        status_code=403,
                    )
                request.session.clear()
                verified_next_url = _safe_next_url(
                    payload.get("next_url"),
                    _signup_created_next_default(),
                )
                verified_next = quote(verified_next_url, safe="")
                return RedirectResponse(
                    url=f"{_resolve_app_base_url(request)}/login?verified=1&next={verified_next}",
                    status_code=303,
                )

            if getattr(user, "deletion_requested_at", None) is not None:
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Účet je zrušený a aktivační odkaz už není platný.",
                    status_code=403,
                )

            user.email_verified_at = utc_now()
            user.is_active = True
            db.add(user)
            _audit_log(
                db,
                request=request,
                action="signup_email_verified",
                entity_type="user",
                entity_id=int(user.id),
                user_id=int(user.id),
                subject_id=None,
                data={"email": email},
            )
            try:
                db.commit()
            except SQLAlchemyError:
                db.rollback()
                return _login_page_context(
                    request=request,
                    next_url="/login",
                    error="Nepodařilo se potvrdit registraci. Zkus to prosím znovu.",
                    status_code=500,
                )
            request.session.clear()
            verified_next_url = _safe_next_url(payload.get("next_url"), _signup_created_next_default())
            verified_next = quote(verified_next_url, safe="")
            return RedirectResponse(url=f"{_resolve_app_base_url(request)}/login?verified=1&next={verified_next}", status_code=303)

        @app.post("/signup")
        async def signup_submit(request: Request, db: Session = Depends(get_db)):
            if not _signup_available():
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            if request.session.get("user_id"):
                return RedirectResponse(url="/", status_code=303)

            _login_rate_limit_or_429(request)
            await _verify_csrf(request)
            form = await _request_form_once(request)
            prefill = _signup_prefill_from_values(form)
            if str(form.get("subject_ico_manual") or "").strip():
                prefill["subject_ico"] = str(form.get("subject_ico_manual") or "").strip()
            next_url = _safe_next_url(form.get("next"), _signup_created_next_default())
            password = str(form.get("password") or "")
            password2 = str(form.get("password2") or "")

            def _signup_error(message: str, *, status_code: int = 400) -> HTMLResponse:
                return _render_signup_page(
                    request,
                    next_url=next_url,
                    prefill=prefill,
                    error=message,
                    status_code=status_code,
                )

            username = prefill["username"]
            email = prefill["email"]
            subject_name = prefill["subject_name"]
            subject_email = prefill["subject_email"] or email
            subject_country = prefill["subject_country"]
            subject_default_currency = prefill["subject_default_currency"]

            if form.get("lookup_registry"):
                registry_prefill = {
                    "subject_name": prefill["subject_name"],
                    "subject_street": prefill["subject_street"],
                    "subject_city": prefill["subject_city"],
                    "subject_zip": prefill["subject_zip"],
                    "subject_country": prefill["subject_country"],
                    "subject_ico": prefill["subject_ico"],
                    "subject_dic": prefill["subject_dic"],
                }
                registry_prefill, info, lookup_error = _lookup_subject_prefill_from_registry(
                    db,
                    prefill=registry_prefill,
                    prefix="subject_",
                )
                prefill["subject_name"] = str(registry_prefill.get("subject_name") or "")
                prefill["subject_street"] = str(registry_prefill.get("subject_street") or "")
                prefill["subject_city"] = str(registry_prefill.get("subject_city") or "")
                prefill["subject_zip"] = str(registry_prefill.get("subject_zip") or "")
                prefill["subject_country"] = str(registry_prefill.get("subject_country") or prefill["subject_country"] or "CZ")
                prefill["subject_ico"] = str(registry_prefill.get("subject_ico") or "")
                prefill["subject_dic"] = str(registry_prefill.get("subject_dic") or "")
                return _render_signup_page(
                    request,
                    next_url=next_url,
                    prefill=prefill,
                    info=info,
                    error=lookup_error,
                    status_code=200 if lookup_error is None else 400,
                )

            if not username or len(username) < 3:
                return _signup_error("Uživatelské jméno musí mít alespoň 3 znaky.")
            if len(username) > 64:
                return _signup_error("Uživatelské jméno může mít nejvýše 64 znaků.")
            if not email or not looks_like_email(email):
                return _signup_error("Zadej platný e-mail účtu.")
            if len(email) > 255:
                return _signup_error("E-mail účtu je příliš dlouhý.")
            password_error = new_password_length_error(password)
            if password_error:
                return _signup_error(password_error)
            if password != password2:
                return _signup_error("Hesla se neshodují.")
            if not subject_name:
                return _signup_error("Vyplň název prvního IČO / subjektu.")
            if len(subject_name) > 255:
                return _signup_error("Název subjektu je příliš dlouhý.")
            if subject_email and not looks_like_email(subject_email):
                return _signup_error("E-mail subjektu musí být platný, nebo ho nech prázdný.")
            if len(subject_email) > 255:
                return _signup_error("E-mail subjektu je příliš dlouhý.")
            if len(subject_country) != 2:
                return _signup_error("Kód země subjektu musí mít 2 znaky.")
            if not _is_supported_signup_subject_country(subject_country):
                return _signup_error("Registrace je teď dostupná jen pro český subjekt / IČO. Odběratele ze zahraničí můžeš do faktur přidávat normálně.")
            if len(subject_default_currency) != 3:
                return _signup_error("Výchozí měna subjektu musí mít 3 znaky.")

            try:
                username_key = username.lower()
                existing = db.scalar(
                    select(User)
                    .where(or_(func.lower(User.username) == username_key, func.lower(User.email) == email))
                    .limit(1)
                )
                if existing is not None:
                    return _signup_error("Uživatelské jméno nebo e-mail už existuje.")

                user = User(
                    username=username,
                    email=email,
                    password_hash=hash_password(password),
                    is_active=False,
                )
                db.add(user)
                db.flush()

                subject = Subject(
                    name=subject_name,
                    email=subject_email,
                    phone=prefill["subject_phone"],
                    street=prefill["subject_street"],
                    city=prefill["subject_city"],
                    zip=prefill["subject_zip"],
                    country=subject_country,
                    ico=prefill["subject_ico"],
                    dic=prefill["subject_dic"],
                    bank_account=prefill["subject_bank_account"],
                    default_currency=subject_default_currency,
                    tax_regime="standard",
                    flat_tax_band="1",
                    flat_tax_income_profile="general",
                )
                db.add(subject)
                db.flush()
                ensure_subject_public_username(db, subject=subject)
                if (getattr(subject, "bank_account", "") or "").strip():
                    _ensure_subject_bank_accounts_bootstrap(db, subject=subject)

                db.add(
                    UserSubject(
                        user_id=int(user.id),
                        subject_id=int(subject.id),
                        role="owner",
                        can_view=True,
                        can_edit=True,
                        can_issue=True,
                        can_export=True,
                    )
                )
                db.flush()
                _audit_log(
                    db,
                    request=request,
                    action="signup_completed",
                    entity_type="user",
                    entity_id=int(user.id),
                    subject_id=int(subject.id),
                    user_id=int(user.id),
                    data={
                        "subject_id": int(subject.id),
                        "subject_name": str(subject.name or ""),
                        "subject_ico": str(subject.ico or ""),
                        "next_url": next_url,
                        "email_verification_required": True,
                    },
                )
                db.commit()
                db.refresh(user)
                db.refresh(subject)
            except SQLAlchemyError:
                db.rollback()
                return _signup_error("Registraci se nepodařilo dokončit. Zkus to prosím znovu.", status_code=500)
            sent_ok, send_error = _send_signup_verification_email(db, request=request, user=user, next_url=next_url)
            if not sent_ok and send_error:
                request.session["signup_pending_email"] = str(
                    getattr(user, "email", "") or email
                )
                return _render_signup_pending_page(
                    request,
                    email=str(getattr(user, "email", "") or email),
                    email_sent=False,
                    next_url=next_url,
                    error=send_error,
                    info="Účet je založený, ale potvrzovací e-mail se teď nepodařilo odeslat. Zkus to prosím znovu tlačítkem níž.",
                    status_code=500,
                )

            request.session["signup_pending_email"] = str(
                getattr(user, "email", "") or email
            )
            return RedirectResponse(
                url=f"/signup/pending?sent=1&next={quote(next_url)}",
                status_code=303,
            )

        def _setup_allowed() -> bool:
            return bool(settings.setup_token)

        def _setup_token_ok(token: str | None) -> bool:
            return bool(settings.setup_token) and secrets.compare_digest(
                str(token or ""),
                str(settings.setup_token or ""),
            )

        @app.get("/setup", response_class=HTMLResponse)
        def setup_page(request: Request, next: str | None = None, token: str | None = None, db: Session = Depends(get_db)):
            if not _setup_allowed():
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            next_url = _safe_next_url(next, "/")
            setup_requires_token = True

            # Allow setup only when there are no users yet.
            try:
                users_count = int(db.scalar(select(func.count(User.id))) or 0)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return templates.TemplateResponse(
                    request,
                    "auth/login.html",
                    {
                        "db_enabled": False,
                        "db_error": _safe_db_error_message(exc),
                        "next_url": next_url,
                        "prefill": {"identifier": ""},
                        "error": "Databáze není dostupná – nelze vytvořit účet.",
                        "setup_available": False,
                        "setup_requires_token": setup_requires_token,
                    },
                    status_code=503,
                )

            if users_count > 0:
                return RedirectResponse(url=f"/login?next={quote(next_url)}", status_code=303)

            return templates.TemplateResponse(
                request,
                "auth/setup.html",
                {
                    "next_url": next_url,
                    "setup_requires_token": setup_requires_token,
                    "prefill": {
                        "token": "",
                        "username": "",
                        "email": "",
                    },
                    "error": None,
                },
            )

        @app.post("/setup")
        async def setup_submit(request: Request, db: Session = Depends(get_db)):
            if not _setup_allowed():
                return JSONResponse(status_code=404, content={"detail": "Not found"})


            # Phase-29: verify CSRF token for account setup.
            await _verify_csrf(request)

            form = await request.form()
            token = (form.get("token") or "").strip() or None
            username = (form.get("username") or "").strip()
            email = (form.get("email") or "").strip()
            password = str(form.get("password") or "")
            password2 = str(form.get("password2") or "")
            next_url = _safe_next_url(form.get("next"), "/")

            setup_requires_token = True

            # Never reflect the setup token back into an HTML response.
            prefill = {"token": "", "username": username, "email": email}

            if not _setup_token_ok(token):
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": "Neplatný setup token.",
                    },
                    status_code=403,
                )

            # Allow setup only when there are no users yet.
            try:
                users_count = int(db.scalar(select(func.count(User.id))) or 0)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": _safe_operation_error(exc, fallback="Databáze není dostupná."),
                    },
                    status_code=503,
                )

            if users_count > 0:
                return RedirectResponse(url="/login", status_code=303)

            if not username or not email or not password:
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": "Vyplň uživatelské jméno, e-mail a heslo.",
                    },
                    status_code=400,
                )

            if password != password2:
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": "Hesla se neshodují.",
                    },
                    status_code=400,
                )

            password_error = new_password_length_error(password)
            if password_error:
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": password_error,
                    },
                    status_code=400,
                )

            if len(username) < 3:
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": "Uživatelské jméno musí mít alespoň 3 znaky.",
                    },
                    status_code=400,
                )

            # Create user + link to default subject.
            user = User(
                username=username,
                email=email,
                password_hash=hash_password(password),
                is_active=True,
                email_verified_at=utc_now(),
                last_login_at=utc_now(),
            )
            db.add(user)
            try:
                db.flush()

                # Ensure the default subject exists (migration seeds id=1).
                subject = db.get(Subject, 1)
                if subject is None:
                    subject = Subject(id=1)
                    db.add(subject)
                    db.flush()

                link = UserSubject(
                    user_id=int(user.id),
                    subject_id=1,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                )
                db.add(link)
                db.flush()
                db.commit()
                db.refresh(user)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                # Likely uniqueness violation.
                return templates.TemplateResponse(
                    request,
                    "auth/setup.html",
                    {
                        "next_url": next_url,
                        "setup_requires_token": setup_requires_token,
                        "prefill": prefill,
                        "error": _safe_operation_error(exc, fallback="Nepodařilo se vytvořit účet."),
                    },
                    status_code=400,
                )

            # Auto-login after setup.
            request.session.clear()
            request.session["user_id"] = int(user.id)
            request.session["username"] = str(user.username)
            request.session["ui_theme"] = _normalize_ui_theme(getattr(user, "ui_theme", "system"))
            request.session["ui_language"] = _normalize_ui_language(getattr(user, "ui_language", "cs"))
            request.session["authenticated_at"] = utc_now().isoformat()
            request.session["session_version"] = int(getattr(user, "session_version", 1) or 1)
            request.session["subject_id"] = 1

            return RedirectResponse(url=next_url, status_code=303)

    else:

        @app.post("/login")
        async def login_submit_no_db(request: Request):
            # Phase-29: apply login rate limiting and CSRF protection even when DB is down.
            _login_rate_limit_or_429(request)
            await _verify_csrf(request)

            next_url = "/"
            try:
                form = await request.form()
                next_url = _safe_next_url(form.get("next"), "/")
            except Exception:
                pass

            return templates.TemplateResponse(
                request,
                "auth/login.html",
                {
                    "db_enabled": False,
                    "db_error": _safe_db_error_message(),
                    "next_url": next_url,
                    "prefill": {"identifier": ""},
                    "error": "Databáze není dostupná – nelze se přihlásit.",
                    "setup_available": False,
                    "setup_requires_token": True,
                },
                status_code=503,
            )

    def _mask_db_url(db_url: str) -> str:
        """Mask password in a SQLAlchemy DB URL for safe display in UI."""

        try:
            parts = urlsplit(db_url)
        except Exception:
            return db_url

        if not parts.scheme or not parts.netloc:
            return db_url

        if parts.username:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"

            userinfo = parts.username
            if parts.password:
                userinfo = f"{userinfo}:***"

            netloc = f"{userinfo}@{host}"
        else:
            netloc = parts.netloc

        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _render_db_disabled(
        request: Request,
        *,
        title: str,
        db_error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render a friendly HTML page when DB features are unavailable."""

        detail = (db_error or _db_import_error or "").strip() or None
        if detail is not None:
            detail = _safe_db_error_message(detail)

        return templates.TemplateResponse(
            request,
            "db_disabled.html",
            {
                "title": title,
                "db_error": detail,
            },
            status_code=status_code,
        )

    def _issuer_from_env() -> dict:
        """Issuer dict from environment (Settings).

        Used as a fallback when DB is disabled or issuer profile isn't stored yet.
        """

        return {
            "name": settings.issuer_name,
            "email": settings.issuer_email,
            "phone": settings.issuer_phone,
            "street": settings.issuer_street,
            "city": settings.issuer_city,
            "zip": settings.issuer_zip,
            "country": settings.issuer_country,
            "ico": settings.issuer_ico,
            "dic": settings.issuer_dic,
            "bank_account": settings.issuer_bank_account,
            "is_vat_payer": False,
            "is_vat_identified_person": False,
            "legal_form": "business",
            "tax_regime": "standard",
            "flat_tax_band": "1",
            "flat_tax_income_profile": "general",
            "default_currency": "CZK",
        }

    COMMON_CURRENCY_OPTIONS: list[tuple[str, str]] = [
        ("CZK", "CZK – Česká koruna"),
        ("EUR", "EUR – Euro"),
        ("USD", "USD – Americký dolar"),
        ("PLN", "PLN – Polský zlotý"),
        ("GBP", "GBP – Britská libra"),
        ("HUF", "HUF – Maďarský forint"),
    ]

    def _build_currency_options(selected_currency: str | None = None) -> list[tuple[str, str]]:
        options = list(COMMON_CURRENCY_OPTIONS)
        current = str(selected_currency or "").strip().upper()
        if current and all(code != current for code, _label in options):
            options.insert(0, (current, current))
        return options


    COMMON_ACCOUNT_COUNTRY_OPTIONS: list[tuple[str, str]] = [
        ("CZ", "CZ – Česká republika"),
        ("SK", "SK – Slovensko"),
    ]

    def _build_account_country_options(selected_country: str | None = None) -> list[tuple[str, str]]:
        options = list(COMMON_ACCOUNT_COUNTRY_OPTIONS)
        current = str(selected_country or "").strip().upper()
        if current and all(code != current for code, _label in options):
            options.insert(0, (current, current))
        return options

    def _attachment_disposition(filename: str) -> str:
        raw = Path((filename or "").strip()).name
        suffix = Path(raw).suffix or ""
        stem = Path(raw).stem or "export"
        safe_unicode = safe_filename_base(stem, fallback="export")
        ascii_stem = (
            unicodedata.normalize("NFKD", safe_unicode)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        safe_ascii = safe_filename_base(ascii_stem, fallback="export")
        ascii_filename = f"{safe_ascii}{suffix}"
        utf8_filename = quote(f"{safe_unicode}{suffix}", safe="")
        return f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{utf8_filename}"

    def _decimal_to_export_str(value: object | None) -> str:
        if value is None:
            return ""
        try:
            return format(Decimal(str(value)), "f")
        except Exception:
            return str(value)

    def _money_cents_to_export_str(value: int | None) -> str:
        cents = int(value or 0)
        sign = "-" if cents < 0 else ""
        abs_cents = abs(cents)
        return f"{sign}{abs_cents // 100}.{abs_cents % 100:02d}"

    def _iso_to_export_str(value: object | None) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            try:
                return value.isoformat(timespec="seconds")
            except TypeError:
                return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    def _csv_bytes_from_rows(fieldnames: list[str], rows: list[dict[str, object]]) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=fieldnames,
            delimiter=";",
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            safe_row: dict[str, str] = {}
            for key in fieldnames:
                value = row.get(key)
                safe_row[key] = csv_safe_cell(_iso_to_export_str(value))
            writer.writerow(safe_row)
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    def _csv_attachment_response(fieldnames: list[str], rows: list[dict[str, object]], *, filename: str) -> Response:
        return Response(
            content=_csv_bytes_from_rows(fieldnames, rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": _attachment_disposition(filename)},
        )

    def _render_settings_page(
        request: Request,
        *,
        issuer: dict,
        issuer_source: str,
        saved: bool = False,
        info: str | None = None,
        error: str | None = None,
        status_code: int = 200,
        current_user: dict[str, object] | None = None,
        bank_accounts: list[dict[str, object]] | None = None,
        account_prefill: dict | None = None,
        current_subject: dict[str, object] | None = None,
        accessible_subjects: list[dict[str, object]] | None = None,
        can_edit_subject: bool = False,
        can_manage_subject: bool = False,
        can_manage_subject_users: bool = False,
        subject_users: list[dict[str, object]] | None = None,
        subject_user_role_options: list[str] | None = None,
        subject_prefill: dict | None = None,
        user_access_prefill: dict | None = None,
        existing_user_link_prefill: dict | None = None,
        password_prefill: dict | None = None,
        api_tokens: list[dict[str, object]] | None = None,
        api_token_prefill: dict | None = None,
        api_token_created: dict[str, str] | None = None,
        account_deletion_summary: dict[str, object] | None = None,
        subject_lookup_done: bool = False,
        active_settings_panel: str | None = None,
        setup_warnings: list[dict[str, object]] | None = None,
        issued_pdf_refresh_count: int = 0,
    ) -> HTMLResponse:
        effective_issuer = dict(issuer or {})
        inferred_footer_mode = "trade_register"
        issuer_name_for_footer = str(effective_issuer.get("name") or "").lower()
        if "z.s." in issuer_name_for_footer or " z.s" in issuer_name_for_footer or "spolek" in issuer_name_for_footer:
            inferred_footer_mode = "association_register"
        elif any(token in issuer_name_for_footer for token in ("s.r.o", "a.s", "k.s", "v.o.s", "sro", "as")):
            inferred_footer_mode = "commercial_register"
        effective_footer_mode = str(
            effective_issuer.get("default_invoice_footer_mode") or inferred_footer_mode
        ).strip().lower()
        if effective_footer_mode not in {value for value, _label in INVOICE_FOOTER_PRESET_OPTIONS}:
            effective_footer_mode = inferred_footer_mode
        effective_issuer["default_invoice_footer_mode"] = effective_footer_mode
        effective_issuer["default_invoice_footer_text"] = str(
            effective_issuer.get("default_invoice_footer_text")
            or _invoice_footer_text_for_mode(effective_footer_mode)
        )
        effective_issuer["tax_regime"] = _normalize_tax_regime(effective_issuer.get("tax_regime"))
        effective_issuer["legal_form"] = _normalize_subject_legal_form(effective_issuer.get("legal_form"))
        if not _subject_uses_business_tax_limits(effective_issuer.get("legal_form")):
            effective_issuer["tax_regime"] = "standard"
        effective_issuer["flat_tax_band"] = _normalize_flat_tax_band(effective_issuer.get("flat_tax_band"))
        effective_issuer["flat_tax_income_profile"] = _normalize_flat_tax_income_profile(
            effective_issuer.get("flat_tax_income_profile")
        )
        effective_issuer["default_invoice_style"] = _normalize_invoice_style(
            effective_issuer.get("default_invoice_style")
        )
        effective_issuer["invoice_pdf_theme"] = normalize_invoice_pdf_theme(
            effective_issuer.get("invoice_pdf_theme")
        )
        effective_issuer["tax_alerts_enabled"] = bool(effective_issuer.get("tax_alerts_enabled"))
        effective_issuer["tax_alert_email"] = str(effective_issuer.get("tax_alert_email") or "").strip()
        effective_account_prefill = account_prefill or {
            "id": "",
            "label": "",
            "account_number": "",
            "iban": "",
            "bic": "",
            "country": effective_issuer.get("country") if isinstance(effective_issuer, dict) else "CZ",
            "currency": effective_issuer.get("default_currency") if isinstance(effective_issuer, dict) else "CZK",
            "is_default": False,
            "payment_sync_provider": "none",
            "payment_sync_enabled": False,
            "payment_sync_auto_pair": True,
            "fio_api_token": "",
            "has_fio_api_token": False,
            "payment_sync_email_sender_filter": "",
            "payment_sync_email_subject_filter": "",
            "payment_sync_email_parser": "pending",
            "payment_sync_last_email_uid": "",
        }
        effective_subject_prefill = subject_prefill or {
            "name": "",
            "email": "",
            "phone": "",
            "street": "",
            "city": "",
            "zip": "",
            "country": effective_issuer.get("country") if isinstance(effective_issuer, dict) else "CZ",
            "ico": "",
            "dic": "",
            "is_vat_payer": False,
            "is_vat_identified_person": False,
            "legal_form": effective_issuer.get("legal_form") if isinstance(effective_issuer, dict) else "business",
            "tax_regime": effective_issuer.get("tax_regime") if isinstance(effective_issuer, dict) else "standard",
            "flat_tax_band": effective_issuer.get("flat_tax_band") if isinstance(effective_issuer, dict) else "1",
            "flat_tax_income_profile": (
                effective_issuer.get("flat_tax_income_profile") if isinstance(effective_issuer, dict) else "general"
            ),
            "default_currency": effective_issuer.get("default_currency") if isinstance(effective_issuer, dict) else "CZK",
            "switch_after_create": True,
        }
        effective_user_access_prefill = user_access_prefill or {
            "username": "",
            "email": "",
            "password": "",
            "role": "manager",
            "can_view": True,
            "can_edit": True,
            "can_issue": True,
        }
        effective_existing_user_link_prefill = existing_user_link_prefill or {
            "identifier": "",
            "role": "manager",
            "can_view": True,
            "can_edit": True,
            "can_issue": True,
        }
        effective_password_prefill = password_prefill or {
            "current_password": "",
            "new_password": "",
            "new_password2": "",
        }
        effective_api_token_prefill = api_token_prefill or {
            "name": "",
            "expires_in_days": "0",
            "subject_id": str((current_subject or {}).get("id") or ""),
            "is_sandbox": False,
        }
        if "subject_id" not in effective_api_token_prefill:
            effective_api_token_prefill["subject_id"] = str((current_subject or {}).get("id") or "")
        if "is_sandbox" not in effective_api_token_prefill:
            effective_api_token_prefill["is_sandbox"] = False
        api_base_url = f"{str(request.base_url).rstrip('/')}/api/v1"
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "app_env": settings.app_env,
                "debug": settings.debug,
                "db_enabled": _db_enabled,
                "database_url_masked": _mask_db_url(settings.database_url),
                "issuer": effective_issuer,
                "issuer_source": issuer_source,
                "saved": saved,
                "info": info,
                "error": error,
                "current_user": current_user or {},
                "session_max_age_options": SESSION_MAX_AGE_OPTIONS,
                "ui_theme_options": UI_THEME_OPTIONS,
                "ui_language_options": UI_LANGUAGE_OPTIONS,
                "tax_regime_options": TAX_REGIME_OPTIONS,
                "subject_legal_form_options": SUBJECT_LEGAL_FORM_OPTIONS,
                "flat_tax_band_options": FLAT_TAX_BAND_OPTIONS,
                "flat_tax_income_profile_options": FLAT_TAX_INCOME_PROFILE_OPTIONS,
                "currency_options": _build_currency_options(effective_issuer.get("default_currency") if isinstance(effective_issuer, dict) else None),
                "invoice_style_options": INVOICE_STYLE_OPTIONS,
                "invoice_pdf_theme_options": INVOICE_PDF_THEME_OPTIONS,
                "invoice_pdf_theme_descriptions": INVOICE_PDF_THEME_DESCRIPTIONS,
                "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                "payment_sync_provider_options": PAYMENT_SYNC_PROVIDER_OPTIONS,
                "payment_sync_email_parser_options": EMAIL_BANK_PARSER_OPTIONS,
                "payment_sync_imap_address": str(getattr(settings, "payment_sync_imap_username", "") or "").strip(),
                "payment_sync_imap_configured": bool(
                    str(getattr(settings, "payment_sync_imap_host", "") or "").strip()
                    and str(getattr(settings, "payment_sync_imap_username", "") or "").strip()
                    and str(getattr(settings, "payment_sync_imap_password", "") or "").strip()
                ),
                "payment_sync_alert_domain": str(getattr(settings, "payment_sync_alert_domain", "") or "").strip().lower(),
                "bank_accounts": list(bank_accounts or []),
                "account_prefill": effective_account_prefill,
                "account_country_options": _build_account_country_options(effective_account_prefill.get("country") if isinstance(effective_account_prefill, dict) else (effective_issuer.get("country") if isinstance(effective_issuer, dict) else None)),
                "current_subject": current_subject or {},
                "accessible_subjects": list(accessible_subjects or []),
                "can_edit_subject": bool(can_edit_subject),
                "can_manage_subject": bool(can_manage_subject),
                "can_manage_subject_users": bool(can_manage_subject_users),
                "subject_users": list(subject_users or []),
                "subject_user_role_options": list(subject_user_role_options or ["manager", "accountant", "viewer"]),
                "subject_prefill": effective_subject_prefill,
                "user_access_prefill": effective_user_access_prefill,
                "existing_user_link_prefill": effective_existing_user_link_prefill,
                "password_prefill": effective_password_prefill,
                "api_tokens": list(api_tokens or []),
                "api_token_prefill": effective_api_token_prefill,
                "api_token_created": api_token_created or {},
                "account_deletion_summary": account_deletion_summary or {},
                "subject_lookup_done": bool(subject_lookup_done),
                "active_settings_panel": active_settings_panel or "",
                "setup_warnings": list(setup_warnings or []),
                "issued_pdf_refresh_count": int(issued_pdf_refresh_count or 0),
                "show_pdf_refresh_action": bool(issued_pdf_refresh_count or 0),
                "next_url": "/settings#issuer",
                "api_token_expiry_options": [
                    ("0", "Bez expirace"),
                    ("30", "30 dní"),
                    ("90", "90 dní"),
                    ("365", "1 rok"),
                ],
                "api_rate_limit_max": int(getattr(settings, "api_rate_limit_max", 240) or 240),
                "api_rate_limit_window_seconds": int(getattr(settings, "api_rate_limit_window_seconds", 60) or 60),
                "api_monthly_quota_max": int(getattr(settings, "api_monthly_quota_max", 2500) or 2500),
                "api_base_url": api_base_url,
                "api_docs_url": "/api/v1/docs",
                "api_openapi_json_url": "/api/v1/openapi.json",
                "api_health_url": "/api/v1/healthz",
            },
            status_code=status_code,
        )

    # ------------------------------------------------------------------
    # Subject context (placeholder)
    # ------------------------------------------------------------------
    #
    # The master plan introduces multi-tenant "subjects" (billing entities).
    # Auth + subject switching comes in later phases. Until then, all MVP
    # routes operate under a single seeded subject id=1.

    DEFAULT_SUBJECT_ID = 1

    def _current_subject_id() -> int:
        """Return the current subject id from the session.

        This helper consults the request stored in the context variable
        `_current_request` and returns the subject id stored in the
        session.  When no request is active or the session is missing
        a subject, the fallback DEFAULT_SUBJECT_ID is returned.
        """
        try:
            req = _current_request.get()  # type: ignore[name-defined]
        except Exception:
            req = None
        if req is not None:
            try:
                sid = req.session.get("subject_id")
                if sid is not None:
                    return int(sid)
            except Exception:
                pass
        return DEFAULT_SUBJECT_ID

    if _db_enabled:
        def _subject_has_profile(subject: Subject) -> bool:
            for key in (
                "name",
                "email",
                "phone",
                "street",
                "city",
                "zip",
                "ico",
                "dic",
                "bank_account",
            ):
                if (getattr(subject, key, "") or "").strip():
                    return True
            return False

        def _issuer_from_subject(subject: Subject) -> dict:
            return {
                "name": subject.name,
                "email": subject.email,
                "phone": subject.phone,
                "street": subject.street,
                "city": subject.city,
                "zip": subject.zip,
                "country": subject.country,
                "ico": subject.ico,
                "dic": subject.dic,
                "bank_account": subject.bank_account,
                "is_vat_payer": bool(subject.is_vat_payer),
                "is_vat_identified_person": bool(getattr(subject, "is_vat_identified_person", False)),
                "legal_form": _normalize_subject_legal_form(getattr(subject, "legal_form", "business")),
                "tax_regime": _normalize_tax_regime(getattr(subject, "tax_regime", "standard")),
                "flat_tax_band": _normalize_flat_tax_band(getattr(subject, "flat_tax_band", "1")),
                "flat_tax_income_profile": _normalize_flat_tax_income_profile(
                    getattr(subject, "flat_tax_income_profile", "general")
                ),
                "tax_alerts_enabled": bool(getattr(subject, "tax_alerts_enabled", False)),
                "tax_alert_email": str(getattr(subject, "tax_alert_email", "") or ""),
                "default_currency": subject.default_currency,
                "default_invoice_style": _normalize_invoice_style(getattr(subject, "default_invoice_style", None)),
                "invoice_pdf_theme": normalize_invoice_pdf_theme(getattr(subject, "invoice_pdf_theme", None)),
                "default_invoice_footer_mode": str(getattr(subject, "default_invoice_footer_mode", "") or ""),
                "default_invoice_footer_text": str(getattr(subject, "default_invoice_footer_text", "") or ""),
            }

        def _issuer_from_legacy_profile(profile: IssuerProfile) -> dict:
            return {
                "name": profile.name,
                "email": profile.email,
                "phone": profile.phone,
                "street": profile.street,
                "city": profile.city,
                "zip": profile.zip,
                "country": profile.country,
                "ico": profile.ico,
                "dic": profile.dic,
                "bank_account": profile.bank_account,
                "is_vat_payer": False,
                "is_vat_identified_person": False,
                "legal_form": "business",
                "tax_regime": "standard",
                "flat_tax_band": "1",
                "flat_tax_income_profile": "general",
                "tax_alerts_enabled": False,
                "tax_alert_email": "",
                "default_currency": "CZK",
                "default_invoice_style": "modern",
                "invoice_pdf_theme": "standard",
            }

        def _current_user_id_or_none(request: Request | None = None) -> int | None:
            req = request
            if req is None:
                try:
                    req = _current_request.get()  # type: ignore[name-defined]
                except Exception:
                    req = None
            if req is None:
                return None
            try:
                raw = req.session.get("user_id")
                return int(raw) if raw is not None else None
            except Exception:
                return None

        def _current_user_settings_view(db: Session, request: Request | None = None) -> dict[str, object]:
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return {}
            try:
                user = db.get(User, int(user_id))
            except SQLAlchemyError:
                return {}
            if user is None:
                return {}
            last_login_at = getattr(user, "last_login_at", None)
            return {
                "id": int(user.id),
                "username": str(getattr(user, "username", "") or ""),
                "email": str(getattr(user, "email", "") or ""),
                "is_active": bool(getattr(user, "is_active", False)),
                "ui_theme": _normalize_ui_theme(getattr(user, "ui_theme", "system")),
                "ui_language": _normalize_ui_language(getattr(user, "ui_language", "cs")),
                "session_max_age_days": _normalize_session_max_age_days(getattr(user, "session_max_age_days", 7)),
                "deletion_requested_at": _format_settings_datetime(getattr(user, "deletion_requested_at", None)),
                "deletion_scheduled_for": _format_settings_datetime(getattr(user, "deletion_scheduled_for", None)),
                "last_login_at": (
                    last_login_at.strftime("%d.%m.%Y %H:%M")
                    if isinstance(last_login_at, datetime)
                    else ""
                ),
            }

        API_TOKEN_EXPIRY_OPTIONS = [
            ("0", "Bez expirace"),
            ("30", "30 dní"),
            ("90", "90 dní"),
            ("365", "1 rok"),
        ]

        def _format_settings_datetime(value: datetime | None) -> str:
            if value is None:
                return ""
            try:
                return value.strftime("%d.%m.%Y %H:%M")
            except Exception:
                return ""

        def _format_settings_date(value: date | None) -> str:
            if value is None:
                return ""
            try:
                return value.strftime("%d.%m.%Y")
            except Exception:
                return ""


        def _api_tokens_view_rows(db: Session, *, user_id: int) -> list[dict[str, object]]:
            now = utc_now()
            tokens = list(
                db.scalars(
                    select(ApiToken)
                    .where(ApiToken.user_id == int(user_id))
                    .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
                )
            )
            today = datetime.now(ZoneInfo("Europe/Prague")).date()
            monthly_usage_rows = db.execute(
                select(ApiTokenMonthlyUsage)
                .where(ApiTokenMonthlyUsage.usage_year == int(today.year))
                .where(ApiTokenMonthlyUsage.usage_month == int(today.month))
                .where(ApiTokenMonthlyUsage.token_id.in_([int(getattr(token, "id", 0) or 0) for token in tokens] or [-1]))
            ).scalars().all()
            monthly_usage_by_token = {
                int(getattr(row, "token_id", 0) or 0): row
                for row in monthly_usage_rows
            }
            monthly_quota_limit = int(getattr(settings, "api_monthly_quota_max", 2500) or 2500)

            def _sort_key(token: ApiToken) -> tuple[int, int, int]:
                revoked_at = getattr(token, "revoked_at", None)
                expires_at = getattr(token, "expires_at", None)
                is_expired = bool(as_utc_aware(expires_at) and as_utc_aware(expires_at) < now and revoked_at is None)
                return (
                    0 if revoked_at is None else 1,
                    0 if not is_expired else 1,
                    -int(getattr(token, "id", 0) or 0),
                )

            tokens.sort(key=_sort_key)

            rows: list[dict[str, object]] = []
            for token in tokens:
                revoked_at = getattr(token, "revoked_at", None)
                expires_at = getattr(token, "expires_at", None)
                subject_id = getattr(token, "subject_id", None)
                subject = db.get(Subject, int(subject_id)) if subject_id is not None else None
                is_expired = bool(as_utc_aware(expires_at) and as_utc_aware(expires_at) < now and revoked_at is None)
                monthly_usage = monthly_usage_by_token.get(int(token.id))
                monthly_used = int(getattr(monthly_usage, "request_count", 0) or 0)
                monthly_remaining = max(0, int(monthly_quota_limit) - monthly_used)
                if revoked_at is not None:
                    status_label = "Odvolaný"
                elif subject_id is None:
                    status_label = "Vyžaduje obnovu"
                elif is_expired:
                    status_label = "Vypršel"
                else:
                    status_label = "Aktivní"
                subject_name = str(getattr(subject, "name", "") or "").strip()
                subject_ico = str(getattr(subject, "ico", "") or "").strip()
                subject_label = subject_name or "Bez přiřazeného subjektu"
                if subject_ico:
                    subject_label = f"{subject_label} • IČO {subject_ico}"
                rows.append(
                    {
                        "id": int(token.id),
                        "name": str(getattr(token, "name", "") or "API token"),
                        "token_prefix": str(getattr(token, "token_prefix", "") or ""),
                        "subject_id": int(subject_id) if subject_id is not None else None,
                        "subject_label": subject_label,
                        "status_label": status_label,
                        "created_at": _format_settings_datetime(getattr(token, "created_at", None)),
                        "last_used_at": _format_settings_datetime(getattr(token, "last_used_at", None)),
                        "expires_at": _format_settings_datetime(expires_at),
                        "revoked_at": _format_settings_datetime(revoked_at),
                        "monthly_used": monthly_used,
                        "monthly_remaining": monthly_remaining,
                        "monthly_limit": int(monthly_quota_limit),
                        "monthly_period_label": f"{int(today.month):02d}/{int(today.year)}",
                        "can_read": bool(getattr(token, "can_read", False)),
                        "can_write": bool(getattr(token, "can_write", False)),
                        "can_issue": bool(getattr(token, "can_issue", False)),
                        "can_export": bool(getattr(token, "can_export", False)),
                        "is_sandbox": bool(getattr(token, "is_sandbox", False)),
                        "can_revoke": revoked_at is None,
                    }
                )
            return rows

        def _mail_identity_context(
            db: Session,
            *,
            subject: Subject | None,
            request: Request | None = None,
        ) -> dict[str, str]:
            current_user = _current_user_settings_view(db, request) if request is not None else {}
            subject_email = (getattr(subject, "email", "") or "").strip() if subject else ""
            subject_name = (getattr(subject, "name", "") or "").strip() if subject else ""
            user_email = str(current_user.get("email") or "").strip()
            username = str(current_user.get("username") or "").strip()

            from_email = (settings.smtp_from_email or subject_email or settings.issuer_email or "").strip()
            from_name = (settings.smtp_from_name or subject_name or settings.issuer_name or "").strip()
            signature_name = (subject_name or settings.issuer_name or from_name or username or "").strip()
            copy_to_self_email = (subject_email or user_email or settings.issuer_email or from_email or "").strip()

            return {
                "from_email": from_email,
                "from_name": from_name,
                "signature_name": signature_name,
                "copy_to_self_email": copy_to_self_email,
            }

        def _json_dumps_safe(value: object) -> str | None:
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                return None

        def _audit_log(
            db: Session,
            *,
            request: Request | None = None,
            action: str,
            entity_type: str | None = None,
            entity_id: int | None = None,
            data: dict | None = None,
            subject_id: int | None = None,
            user_id: int | None = None,
        ) -> None:
            ip: str | None = None
            user_agent: str | None = None
            audit_data: dict | None = dict(data) if isinstance(data, dict) else data
            if request is not None:
                try:
                    ip = str(_client_ip(request) or "").strip() or None
                except Exception:
                    ip = None
                try:
                    user_agent = str(request.headers.get("user-agent") or "").strip() or None
                except Exception:
                    user_agent = None
                if user_agent is not None:
                    user_agent = user_agent[:255] or None
                try:
                    request_id = str(getattr(request.state, "request_id", "") or "").strip()
                except Exception:
                    request_id = ""
                if request_id:
                    if audit_data is None:
                        audit_data = {"request_id": request_id}
                    else:
                        audit_data.setdefault("request_id", request_id)

            db.add(
                AuditLog(
                    subject_id=(int(subject_id) if subject_id is not None else _current_subject_id()),
                    user_id=(int(user_id) if user_id is not None else _current_user_id_or_none()),
                    action=str(action or ""),
                    entity_type=entity_type,
                    entity_id=(int(entity_id) if entity_id is not None else None),
                    data_json=_json_dumps_safe(audit_data or None),
                    ip=ip,
                    user_agent=user_agent,
                )
            )


        def _parse_audit_data(row: AuditLog) -> dict:
            try:
                return json.loads(str(getattr(row, "data_json", "") or ""))
            except Exception:
                return {}

        def _load_invoice_audit_entries(db: Session, *, invoice_id: int, subject_id: int) -> list[dict[str, object]]:
            try:
                rows = db.scalars(
                    select(AuditLog)
                    .where(AuditLog.subject_id == int(subject_id))
                    .where(AuditLog.entity_type == "invoice")
                    .where(AuditLog.entity_id == int(invoice_id))
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                ).all()
            except SQLAlchemyError:
                return []
            try:
                current_invoice_number = str(
                    db.scalar(select(Invoice.number).where(Invoice.id == int(invoice_id))) or ""
                ).strip().upper()
            except SQLAlchemyError:
                current_invoice_number = ""

            entries: list[dict[str, object]] = []
            for row in rows:
                data = _parse_audit_data(row)
                action = str(getattr(row, "action", "") or "")
                title = action
                detail_parts: list[str] = []
                if action == "invoice_created":
                    if data.get("copied_from_number"):
                        title = "Kopie dokladu vytvořena"
                        detail_parts.append(f"kopie z {data['copied_from_number']}")
                    else:
                        title = "Koncept vytvořen"
                    if data.get("public_url"):
                        detail_parts.append("veřejný odkaz připraven")
                elif action == "invoice_updated":
                    title = "Faktura upravena"
                    changed = list(data.get("changed_fields") or [])
                    if changed:
                        detail_parts.append("změněno: " + ", ".join(str(x) for x in changed))
                    if data.get("items_changed"):
                        detail_parts.append("upraveny položky")
                    if data.get("total_before_cents") != data.get("total_after_cents"):
                        try:
                            before = format_cents(int(data.get("total_before_cents") or 0), str(data.get("currency") or "CZK"))
                            after = format_cents(int(data.get("total_after_cents") or 0), str(data.get("currency") or "CZK"))
                            detail_parts.append(f"celkem {before} → {after}")
                        except Exception:
                            pass
                elif action == "invoice_duplicate_updated":
                    title = "Zduplikovaný doklad doplněn"
                    changed = list(data.get("changed_fields") or [])
                    if changed:
                        detail_parts.append("změněno: " + ", ".join(str(x) for x in changed))
                    if data.get("items_changed"):
                        detail_parts.append("upraveny položky")
                    if data.get("total_before_cents") != data.get("total_after_cents"):
                        try:
                            before = format_cents(int(data.get("total_before_cents") or 0), str(data.get("currency") or "CZK"))
                            after = format_cents(int(data.get("total_after_cents") or 0), str(data.get("currency") or "CZK"))
                            detail_parts.append(f"celkem {before} → {after}")
                        except Exception:
                            pass
                elif action == "invoice_duplicate_issued":
                    title = "Kopie dokladu vystavena"
                    if data.get("number"):
                        detail_parts.append(f"číslo {data['number']}")
                    if data.get("copied_from_number"):
                        detail_parts.append(f"kopie z {data['copied_from_number']}")
                elif action == "invoice_issued":
                    title = "Faktura vystavena"
                    if data.get("number"):
                        detail_parts.append(f"číslo {data['number']}")
                elif action == "invoice_status_changed":
                    source = str(data.get("from") or "")
                    target = str(data.get("to") or "")
                    if target == "paid":
                        title = "Faktura označena jako zaplacená"
                        if data.get("paid_on"):
                            detail_parts.append(f"datum úhrady {data['paid_on']}")
                    elif data.get("unpaid"):
                        title = "Faktura označena jako nezaplacená"
                        if data.get("previous_paid_on"):
                            detail_parts.append(f"původní datum úhrady {data['previous_paid_on']}")
                    elif target == "sent":
                        if source == "paid":
                            title = "Faktura vrácena na odeslanou"
                        elif source == "cancelled":
                            title = "Faktura obnovena do odeslané"
                        else:
                            title = "Faktura označena jako odeslaná"
                    elif target == "issued":
                        if source == "cancelled":
                            title = "Faktura obnovena do vystavené"
                        else:
                            title = "Faktura vrácena na vystavenou"
                    elif target == "cancelled":
                        title = "Faktura stornována"
                    elif target == "draft":
                        title = "Faktura vrácena na koncept"
                    else:
                        title = "Změna stavu faktury"
                    if data.get("from") or data.get("to"):
                        detail_parts.append(f"{data.get('from') or '—'} → {data.get('to') or '—'}")
                elif action == "invoice_public_rotated":
                    title = "Veřejný odkaz obnoven"
                elif action == "invoice_public_disabled":
                    title = "Veřejný odkaz vypnut"
                elif action == "invoice_public_enabled":
                    title = "Veřejný odkaz zapnut"
                elif action == "invoice_email_sent":
                    title = "Faktura odeslána e-mailem"
                    if data.get("to_email"):
                        detail_parts.append(f"komu: {data['to_email']}")
                elif action == "invoice_reminder_sent":
                    title = "Upomínka odeslána"
                    if data.get("to_email"):
                        detail_parts.append(f"komu: {data['to_email']}")
                elif action == "invoice_bank_sync_match_corrected":
                    title = "Chybné automatické spárování opraveno"
                    if data.get("previous_booked_on") and data.get("booked_on"):
                        detail_parts.append(
                            f"datum úhrady {data['previous_booked_on']} → {data['booked_on']}"
                        )
                    if data.get("reason"):
                        detail_parts.append(str(data["reason"]))
                elif action == "invoice_paid_bank_sync":
                    provider = str(data.get("provider") or "").strip().lower()
                    if provider == "fio_api":
                        title = "Platba spárována přes Fio API"
                    elif provider == "email_bank_csas_cz":
                        title = "Platba spárována z e-mailu České spořitelny"
                    elif provider == "email_bank_csob_cz":
                        title = "Platba spárována z e-mailu ČSOB"
                    elif provider == "email_bank_fio_email_cz":
                        title = "Platba spárována z e-mailu Fio banky"
                    elif provider == "email_bank_raiffeisenbank_cz":
                        title = "Platba spárována z e-mailu Raiffeisenbank"
                    else:
                        title = "Platba spárována automaticky"
                    bank_row: BankTransaction | None = None
                    external_id = str(data.get("external_id") or "").strip()
                    if external_id:
                        try:
                            bank_row = db.scalar(
                                select(BankTransaction)
                                .where(BankTransaction.matched_invoice_id == int(invoice_id))
                                .where(BankTransaction.provider == provider)
                                .where(BankTransaction.external_id == external_id)
                                .order_by(BankTransaction.id.desc())
                            )
                        except SQLAlchemyError:
                            bank_row = None
                    if bank_row is None:
                        try:
                            bank_row = db.scalar(
                                select(BankTransaction)
                                .where(BankTransaction.matched_invoice_id == int(invoice_id))
                                .where(BankTransaction.provider == provider)
                                .order_by(BankTransaction.id.desc())
                            )
                        except SQLAlchemyError:
                            bank_row = None

                    amount_cents = data.get("amount_cents")
                    if amount_cents is None and bank_row is not None:
                        amount_cents = getattr(bank_row, "amount_cents", None)
                    currency = str(
                        data.get("currency")
                        or (getattr(bank_row, "currency", None) if bank_row is not None else None)
                        or "CZK"
                    )
                    try:
                        if amount_cents is not None:
                            detail_parts.append(f"částka {format_cents(int(amount_cents), currency)}")
                    except Exception:
                        pass
                    booked_on = data.get("booked_on") or (getattr(bank_row, "booked_on", None) if bank_row is not None else None)
                    if booked_on:
                        detail_parts.append(f"datum připsání {booked_on}")
                    normalized_vs = digits_only(
                        str(
                            data.get("variable_symbol")
                            or (getattr(bank_row, "variable_symbol", None) if bank_row is not None else "")
                        )
                    )[:10]
                    payment_message = normalize_spaces(
                        str(
                            data.get("message")
                            or (getattr(bank_row, "message", None) if bank_row is not None else "")
                        )
                    )
                    matched_by_invoice_number = bool(
                        not normalized_vs
                        and payment_message
                        and current_invoice_number
                        and current_invoice_number in payment_message.upper()
                    )
                    if normalized_vs:
                        detail_parts.append(f"VS {normalized_vs}")
                    elif matched_by_invoice_number:
                        if provider == "fio_api":
                            detail_parts.append(
                                f"Fio API nevrátilo VS; spárováno podle čísla faktury {current_invoice_number} v poznámce"
                            )
                        else:
                            detail_parts.append(
                                f"V notifikaci chyběl VS; spárováno podle čísla faktury {current_invoice_number} v textu platby"
                            )
                    else:
                        detail_parts.append("bez variabilního symbolu")
                    counterparty_name = data.get("counterparty_name") or (
                        getattr(bank_row, "counterparty_name", None) if bank_row is not None else None
                    )
                    if counterparty_name:
                        detail_parts.append(f"protistrana {counterparty_name}")
                    counterparty_account = data.get("counterparty_account") or (
                        getattr(bank_row, "counterparty_account", None) if bank_row is not None else None
                    )
                    if counterparty_account:
                        detail_parts.append(f"účet {counterparty_account}")
                    if payment_message:
                        detail_parts.append(f"zpráva {payment_message}")

                entries.append(
                    {
                        "created_at": getattr(row, "created_at", None),
                        "title": title,
                        "details": "; ".join(part for part in detail_parts if part),
                        "user_id": getattr(row, "user_id", None),
                    }
                )
            return entries

        def _sync_subject_legacy_bank_account(subject: Subject, accounts: list[SubjectBankAccount] | None) -> None:
            ordered = list(accounts or [])
            default_account = next((acc for acc in ordered if bool(getattr(acc, "is_default", False))), None)
            selected = default_account or (ordered[0] if ordered else None)
            subject.bank_account = str(getattr(selected, "account_number", "") or getattr(selected, "iban", "") or "")

        def _ensure_subject_bank_accounts_bootstrap(db: Session, *, subject: Subject | None) -> list[SubjectBankAccount]:
            if subject is None:
                return []

            rows = db.scalars(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.subject_id == int(subject.id))
                .order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
            ).all()
            if rows:
                return rows

            legacy_account = (getattr(subject, "bank_account", "") or "").strip()
            if not legacy_account:
                return []

            try:
                payload = resolve_bank_account(
                    account_number=legacy_account,
                    country=(getattr(subject, "country", "") or "CZ"),
                    label="Hlavní účet",
                )
            except ValueError:
                payload = BankAccountPayload(
                    label="Hlavní účet",
                    number=legacy_account,
                    iban="",
                    bic="",
                    country=(getattr(subject, "country", "") or "CZ"),
                )

            account = SubjectBankAccount(
                subject_id=int(subject.id),
                label=payload.label,
                account_number=payload.number,
                iban=payload.iban or None,
                bic=payload.bic or None,
                country=payload.country or "CZ",
                currency=str(getattr(subject, "default_currency", None) or "CZK").strip().upper() or "CZK",
                is_default=True,
                sort_order=1,
            )
            db.add(account)
            try:
                db.flush()
                rows = [account]
            except SQLAlchemyError:
                db.rollback()
                rows = []
            _sync_subject_legacy_bank_account(subject, rows)
            return rows

        def _list_subject_bank_accounts(db: Session, *, subject_id: int, ensure_bootstrap: bool = True) -> list[SubjectBankAccount]:
            try:
                subject = db.get(Subject, int(subject_id)) if ensure_bootstrap else None
                if ensure_bootstrap and subject is not None:
                    boot = _ensure_subject_bank_accounts_bootstrap(db, subject=subject)
                    if boot:
                        try:
                            db.flush()
                        except SQLAlchemyError:
                            pass
                return db.scalars(
                    select(SubjectBankAccount)
                    .where(SubjectBankAccount.subject_id == int(subject_id))
                    .order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
                ).all()
            except SQLAlchemyError:
                return []

        def _set_default_subject_bank_account(db: Session, *, subject_id: int, account_id: int) -> SubjectBankAccount | None:
            rows = _list_subject_bank_accounts(db, subject_id=int(subject_id))
            selected: SubjectBankAccount | None = None
            for idx, row in enumerate(rows, start=1):
                is_selected = int(row.id) == int(account_id)
                row.is_default = bool(is_selected)
                row.sort_order = 0 if is_selected else idx
                if is_selected:
                    selected = row
                db.add(row)
            subject = db.get(Subject, int(subject_id))
            if subject is not None:
                _sync_subject_legacy_bank_account(subject, rows)
                db.add(subject)
            return selected

        def _default_subject_bank_account(
            db: Session,
            *,
            subject_id: int,
            currency: str | None = None,
        ) -> SubjectBankAccount | None:
            rows = _list_subject_bank_accounts(db, subject_id=int(subject_id))
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

        def _invoice_bank_account_payload(invoice: Invoice, *, subject: Subject | None) -> BankAccountPayload | None:
            number = (getattr(invoice, "bank_account_number", None) or "").strip()
            iban = (getattr(invoice, "bank_account_iban", None) or "").strip()
            bic = (getattr(invoice, "bank_account_bic", None) or "").strip()
            country = (getattr(invoice, "bank_account_country", None) or "").strip() or (getattr(subject, "country", None) or "CZ")
            label = (getattr(invoice, "bank_account_label", None) or "").strip()
            if number or iban:
                try:
                    return resolve_bank_account(
                        account_number=number,
                        iban=iban,
                        bic=bic,
                        country=country,
                        label=label,
                    )
                except ValueError:
                    return BankAccountPayload(label=label or "Bankovní účet", number=number, iban=iban, bic=bic, country=country)

            fallback_raw = (getattr(subject, "bank_account", "") or "").strip() if subject else ""
            if not fallback_raw:
                return None
            try:
                return resolve_bank_account(
                    account_number=fallback_raw,
                    country=(getattr(subject, "country", None) or "CZ") if subject else "CZ",
                    label="Hlavní účet",
                )
            except ValueError:
                return BankAccountPayload(label="Bankovní účet", number=fallback_raw, iban="", bic="", country=(getattr(subject, "country", None) or "CZ") if subject else "CZ")

        def _apply_invoice_bank_account_snapshot(
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
                # keep historical snapshot when a linked account no longer exists
                return
            elif allow_subject_fallback:
                payload = _invoice_bank_account_payload(invoice, subject=subject)
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

        def _build_bank_account_options(
            accounts: list[SubjectBankAccount] | None,
            *,
            current_invoice: Invoice | None = None,
        ) -> list[dict[str, object]]:
            options: list[dict[str, object]] = []
            current_snapshot = None
            if current_invoice is not None:
                current_snapshot = _invoice_bank_account_payload(current_invoice, subject=None)
            for row in list(accounts or []):
                label = (getattr(row, "label", "") or "").strip() or (getattr(row, "account_number", "") or getattr(row, "iban", "") or "Účet")
                number = (getattr(row, "account_number", "") or "").strip()
                iban = (getattr(row, "iban", "") or "").strip()
                display = number or format_iban_for_display(iban) or label
                options.append(
                    {
                        "id": str(int(row.id)),
                        "label": label,
                        "display": display,
                        "currency": str(getattr(row, "currency", "") or "CZK"),
                        "is_default": bool(getattr(row, "is_default", False)),
                    }
                )
            if current_invoice is not None and current_invoice.bank_account_id is None and current_snapshot is not None:
                options.insert(
                    0,
                    {
                        "id": "snapshot",
                        "label": current_snapshot.label or "Historický účet na faktuře",
                        "display": current_snapshot.display,
                        "currency": "",
                        "is_default": False,
                    },
                )
            return options

        ISSUED_INVOICE_REFRESH_STATUSES = ("issued", "sent", "paid")

        def _subject_missing_billing_fields(subject: Subject | None) -> list[str]:
            """Return human-readable issuer fields that are still missing."""

            required_fields = (
                ("name", "název"),
                ("street", "ulice"),
                ("city", "město"),
                ("zip", "PSČ"),
                ("country", "země"),
            )
            if subject is None:
                return [label for _field, label in required_fields]

            missing = [
                label
                for field, label in required_fields
                if not str(getattr(subject, field, "") or "").strip()
            ]
            if _subject_uses_business_tax_limits(getattr(subject, "legal_form", "business")) and not str(getattr(subject, "ico", "") or "").strip():
                missing.append("IČO")
            if bool(getattr(subject, "is_vat_payer", False)) and not str(getattr(subject, "dic", "") or "").strip():
                missing.append("DIČ")
            return missing

        def _subject_has_invoice_bank_account(db: Session, *, subject: Subject | None) -> bool:
            if subject is None:
                return False
            try:
                accounts = _list_subject_bank_accounts(db, subject_id=int(subject.id))
            except Exception:
                accounts = []
            for account in accounts:
                if str(getattr(account, "account_number", "") or "").strip():
                    return True
                if str(getattr(account, "iban", "") or "").strip():
                    return True
            return bool(str(getattr(subject, "bank_account", "") or "").strip())

        def _count_refreshable_issued_invoices(db: Session, *, subject_id: int) -> int:
            try:
                return int(
                    db.scalar(
                        select(func.count(Invoice.id))
                        .where(Invoice.subject_id == int(subject_id))
                        .where(Invoice.status.in_(ISSUED_INVOICE_REFRESH_STATUSES))
                        .where(_invoice_visible_in_lists_clause())
                    )
                    or 0
                )
            except Exception:
                return 0

        def _subject_setup_warnings(
            db: Session,
            *,
            subject: Subject | None,
            require_bank_account: bool = True,
        ) -> list[dict[str, object]]:
            warnings: list[dict[str, object]] = []
            missing_billing = _subject_missing_billing_fields(subject)
            if missing_billing:
                warnings.append(
                    {
                        "code": "missing_billing_profile",
                        "title": "Chybí fakturační údaje vystavovatele",
                        "message": "Doplň fakturační údaje v nastavení. Na vystavené faktuře se pak nebudou lámat údaje napůl ani mizet povinné informace.",
                        "missing": ", ".join(missing_billing),
                        "url": "/settings#issuer",
                        "action_label": "Doplnit údaje",
                    }
                )
            if require_bank_account and not _subject_has_invoice_bank_account(db, subject=subject):
                warnings.append(
                    {
                        "code": "missing_bank_account",
                        "title": "Chybí bankovní účet",
                        "message": "Přidej účet v nastavení. Nové faktury ho předvyberou rovnou v hlavní části formuláře, ne schovaný v dalších možnostech.",
                        "missing": "bankovní účet / IBAN",
                        "url": "/settings#add-bank-account",
                        "action_label": "Přidat účet",
                    }
                )
            return warnings

        def _url_with_query_param(url: str, **params: str) -> str:
            parts = urlsplit(str(url or "/"))
            query_pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
            for key, value in params.items():
                if value is not None:
                    query_pairs[key] = value
            return urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path or "/",
                    urlencode(query_pairs),
                    parts.fragment,
                )
            )

        def _apply_issuer_to_subject(subject: Subject, issuer: dict) -> None:
            subject.name = str(issuer.get("name") or "")
            subject.email = str(issuer.get("email") or "")
            subject.phone = str(issuer.get("phone") or "")
            subject.street = str(issuer.get("street") or "")
            subject.city = str(issuer.get("city") or "")
            subject.zip = str(issuer.get("zip") or "")
            subject.country = str(issuer.get("country") or "CZ")
            subject.ico = str(issuer.get("ico") or "")
            subject.dic = str(issuer.get("dic") or "")
            subject.bank_account = str(issuer.get("bank_account") or "")

            if "is_vat_payer" in issuer:
                subject.is_vat_payer = bool(issuer.get("is_vat_payer"))

            if "is_vat_identified_person" in issuer:
                subject.is_vat_identified_person = bool(issuer.get("is_vat_identified_person"))

            if "legal_form" in issuer:
                subject.legal_form = _normalize_subject_legal_form(issuer.get("legal_form"))

            if "tax_regime" in issuer:
                subject.tax_regime = _normalize_tax_regime(issuer.get("tax_regime"))

            if "flat_tax_band" in issuer:
                subject.flat_tax_band = _normalize_flat_tax_band(issuer.get("flat_tax_band"))

            if "flat_tax_income_profile" in issuer:
                subject.flat_tax_income_profile = _normalize_flat_tax_income_profile(
                    issuer.get("flat_tax_income_profile")
                )

            if "tax_alerts_enabled" in issuer:
                subject.tax_alerts_enabled = bool(issuer.get("tax_alerts_enabled"))

            if "tax_alert_email" in issuer:
                subject.tax_alert_email = str(issuer.get("tax_alert_email") or "") or None

            if "default_currency" in issuer:
                cur = str(issuer.get("default_currency") or "CZK").strip().upper()
                subject.default_currency = cur if len(cur) == 3 else "CZK"

            if "default_invoice_style" in issuer:
                subject.default_invoice_style = _normalize_invoice_style(issuer.get("default_invoice_style"))

            if "invoice_pdf_theme" in issuer:
                subject.invoice_pdf_theme = normalize_invoice_pdf_theme(issuer.get("invoice_pdf_theme"))
                subject.default_invoice_style = _normalize_invoice_style(pdf_theme_to_invoice_style(subject.invoice_pdf_theme))

            if "default_invoice_footer_mode" in issuer:
                footer_mode = str(issuer.get("default_invoice_footer_mode") or "").strip().lower()
                if footer_mode not in {value for value, _label in INVOICE_FOOTER_PRESET_OPTIONS}:
                    footer_mode = _default_invoice_footer_mode(subject)
                subject.default_invoice_footer_mode = footer_mode

            if "default_invoice_footer_text" in issuer:
                subject.default_invoice_footer_text = str(issuer.get("default_invoice_footer_text") or "") or None

        def _ensure_default_subject(db: Session) -> Subject | None:
            """Ensure the seeded default subject exists.

            The migration seeds subject id=1, but we keep this defensive helper
            to make the app more robust when running against older DBs.
            """

            try:
                subject = db.get(Subject, _current_subject_id())
            except SQLAlchemyError:
                return None

            if subject is not None:
                return subject

            # Best effort: create a minimal subject row.
            issuer = _issuer_from_env()
            subject = Subject(
                id=_current_subject_id(),
                name=str(issuer.get("name") or ""),
                email=str(issuer.get("email") or ""),
                phone=str(issuer.get("phone") or ""),
                street=str(issuer.get("street") or ""),
                city=str(issuer.get("city") or ""),
                zip=str(issuer.get("zip") or ""),
                country=str(issuer.get("country") or "CZ"),
                ico=str(issuer.get("ico") or ""),
                dic=str(issuer.get("dic") or ""),
                bank_account=str(issuer.get("bank_account") or ""),
            )
            db.add(subject)
            try:
                db.commit()
                db.refresh(subject)
                return subject
            except SQLAlchemyError:
                db.rollback()
                return None

        def _load_issuer_for_current_subject(db: Session) -> tuple[dict, str]:
            """Return (issuer_dict, source).

            Source is one of: "subject", "legacy", "env".
            """

            issuer_env = _issuer_from_env()

            # Prefer subject profile (master plan), fallback to legacy issuer_profiles,
            # then env.
            try:
                subject = _ensure_default_subject(db)
                if subject is not None and _subject_has_profile(subject):
                    return _issuer_from_subject(subject), "subject"

                profile = db.scalar(select(IssuerProfile).order_by(IssuerProfile.id.asc()).limit(1))
                if profile is not None:
                    issuer = _issuer_from_legacy_profile(profile)

                    # Best effort migration: if the subject exists but is blank,
                    # copy the legacy data over so newer code can rely on subjects.
                    if subject is not None and not _subject_has_profile(subject):
                        _apply_issuer_to_subject(subject, issuer)
                        try:
                            db.commit()
                        except SQLAlchemyError:
                            db.rollback()

                    return issuer, "legacy"
            except SQLAlchemyError:
                # Keep UI usable even if DB queries fail.
                return issuer_env, "env"

            return issuer_env, "env"

        def _load_subject_for_current_session(db: Session) -> Subject | None:
            """Load the current Subject (seller) for this session.

            Returns None on DB errors.
            """

            return _ensure_default_subject(db)

        STATS_MONTH_LABELS = [
            "Leden",
            "Únor",
            "Březen",
            "Duben",
            "Květen",
            "Červen",
            "Červenec",
            "Srpen",
            "Září",
            "Říjen",
            "Listopad",
            "Prosinec",
        ]

        def _subject_chart_currency(subject: Subject | None) -> str:
            return str(getattr(subject, "default_currency", None) or "CZK").strip().upper() or "CZK"

        def _month_start(value: date) -> date:
            return date(int(value.year), int(value.month), 1)

        def _add_months_to_month_start(base: date, months: int) -> date:
            total_months = (int(base.year) * 12 + (int(base.month) - 1)) + int(months)
            year = total_months // 12
            month = (total_months % 12) + 1
            return date(year, month, 1)

        def _build_trailing_twelve_months_context(
            invoice_rows: list[Invoice],
            *,
            chart_currency: str,
            today_value: date,
            vat_view: str = "gross",
        ) -> dict[str, object]:
            current_month_start = _month_start(today_value)
            month_starts = [_add_months_to_month_start(current_month_start, offset) for offset in range(-11, 1)]
            month_index = {
                (int(month_start.year), int(month_start.month)): idx
                for idx, month_start in enumerate(month_starts)
            }
            invoice_buckets = [
                {"month_start": month_start, "invoiced_cents": 0, "paid_cents": 0, "invoice_count": 0, "paid_count": 0}
                for month_start in month_starts
            ]

            valid_invoice_rows = []
            for row in invoice_rows:
                document_type = _normalize_invoice_document_type(getattr(row, "document_type", "invoice"))
                status_value = str(getattr(row, "status", "") or "").strip().lower()
                if document_type != "invoice" or status_value in {"draft", "cancelled"}:
                    continue
                valid_invoice_rows.append(row)

            for row in valid_invoice_rows:
                currency = str(getattr(row, "currency", "") or "CZK").strip().upper() or "CZK"
                if currency != chart_currency:
                    continue

                issue_date = getattr(row, "issue_date", None)
                if isinstance(issue_date, date):
                    bucket_idx = month_index.get((int(issue_date.year), int(issue_date.month)))
                    if bucket_idx is not None:
                        invoice_buckets[bucket_idx]["invoiced_cents"] += _invoice_amount_for_vat_view(row, vat_view)
                        invoice_buckets[bucket_idx]["invoice_count"] += 1

                paid_on = getattr(row, "paid_on", None)
                if str(getattr(row, "status", "") or "").strip().lower() == "paid" and isinstance(paid_on, date):
                    bucket_idx = month_index.get((int(paid_on.year), int(paid_on.month)))
                    if bucket_idx is not None:
                        invoice_buckets[bucket_idx]["paid_cents"] += _invoice_amount_for_vat_view(row, vat_view)
                        invoice_buckets[bucket_idx]["paid_count"] += 1

            max_value = max(
                [
                    max(int(bucket.get("invoiced_cents", 0)), int(bucket.get("paid_cents", 0)))
                    for bucket in invoice_buckets
                ]
                or [0]
            )
            rows = []
            total_invoiced_cents = 0
            total_paid_cents = 0
            total_invoice_count = 0
            total_paid_count = 0
            for bucket in invoice_buckets:
                invoiced_cents = int(bucket["invoiced_cents"])
                paid_cents = int(bucket["paid_cents"])
                total_invoiced_cents += invoiced_cents
                total_paid_cents += paid_cents
                total_invoice_count += int(bucket["invoice_count"])
                total_paid_count += int(bucket["paid_count"])
                month_start = bucket["month_start"]
                rows.append(
                    {
                        "month_start": month_start,
                        "label": f"{STATS_MONTH_LABELS[int(month_start.month) - 1]} {int(month_start.year)}",
                        "short_label": f"{STATS_MONTH_LABELS[int(month_start.month) - 1][:3]}. {str(int(month_start.year))[-2:]}",
                        "invoiced_cents": invoiced_cents,
                        "paid_cents": paid_cents,
                        "invoice_count": int(bucket["invoice_count"]),
                        "paid_count": int(bucket["paid_count"]),
                        "invoiced_bar_percent": 0 if max_value <= 0 else max(8, int(round((invoiced_cents / max_value) * 100))),
                        "paid_bar_percent": 0 if max_value <= 0 else max(8, int(round((paid_cents / max_value) * 100))),
                    }
                )

            return {
                "window_start": month_starts[0],
                "window_end": today_value,
                "rows": rows,
                "total_invoiced_cents": int(total_invoiced_cents),
                "total_paid_cents": int(total_paid_cents),
                "invoice_count": int(total_invoice_count),
                "paid_count": int(total_paid_count),
            }

        def _build_invoice_stats_context(
            db: Session,
            *,
            subject: Subject | None,
            selected_year: int | None = None,
            vat_view: str = "gross",
        ) -> dict[str, object]:
            current_year = date.today().year
            if subject is None:
                return {
                    "selected_year": current_year,
                    "available_years": [current_year],
                    "chart_currency": "CZK",
                    "vat_view": "gross",
                    "vat_view_label": "celkem",
                    "is_vat_payer": False,
                    "yearly_revenue": [],
                    "monthly_revenue": [],
                    "selected_year_total_cents": 0,
                    "selected_year_invoice_count": 0,
                    "totals_by_currency": [],
                    "invoices_by_status": [],
                    "overdue_count": 0,
                    "invoices_count": 0,
                    "contacts_count": 0,
                    "foreign_currency_invoice_count": 0,
                    "trailing_twelve_months": {
                        "window_start": date(current_year, 1, 1),
                        "window_end": date.today(),
                        "rows": [],
                        "total_invoiced_cents": 0,
                        "total_paid_cents": 0,
                        "invoice_count": 0,
                        "paid_count": 0,
                    },
                }

            sid = int(subject.id)
            today = date.today()
            chart_currency = _subject_chart_currency(subject)
            is_vat_payer = bool(getattr(subject, "is_vat_payer", False))
            vat_view = str(vat_view or "gross").strip().lower()
            if not is_vat_payer:
                vat_view = "gross"
            elif vat_view not in {"gross", "net", "vat"}:
                vat_view = "gross"
            vat_view_label = (
                {"gross": "včetně DPH", "net": "bez DPH", "vat": "pouze DPH"}[vat_view]
                if is_vat_payer
                else "celkem"
            )
            use_item_amounts = vat_view in {"net", "vat"}
            item_amount_column = InvoiceItem.line_net_cents if vat_view == "net" else InvoiceItem.line_vat_cents
            year_expr = func.extract("year", Invoice.issue_date)
            month_expr = func.extract("month", Invoice.issue_date)

            visible_filters = (
                Invoice.subject_id == sid,
                _invoice_visible_in_lists_clause(),
            )
            non_draft_filters = (*visible_filters, Invoice.status != "draft", Invoice.issue_date.is_not(None))

            def _amount_sum_expr():
                if use_item_amounts:
                    return func.coalesce(func.sum(item_amount_column), 0)
                return func.coalesce(func.sum(Invoice.total_cents), 0)

            def _apply_amount_join(stmt):
                if use_item_amounts:
                    return stmt.outerjoin(InvoiceItem, InvoiceItem.invoice_id == Invoice.id)
                return stmt

            contacts_count = int(db.scalar(select(func.count(Contact.id)).where(Contact.subject_id == sid)) or 0)
            invoices_count = int(db.scalar(select(func.count(Invoice.id)).where(*visible_filters)) or 0)
            overdue_count = int(
                db.scalar(
                    select(func.count(Invoice.id))
                    .where(*visible_filters)
                    .where(Invoice.due_date < today)
                    .where(Invoice.status != "paid")
                )
                or 0
            )

            available_year_values = db.scalars(
                select(year_expr)
                .where(*non_draft_filters)
                .group_by(year_expr)
                .order_by(year_expr.desc())
            ).all()
            available_years = sorted({int(value) for value in available_year_values if value is not None} | {today.year}, reverse=True)
            if not available_years:
                available_years = [today.year]
            effective_year = int(selected_year) if selected_year in available_years else int(available_years[0])

            yearly_stmt = select(
                year_expr.label("year"),
                _amount_sum_expr().label("total_cents"),
                func.count(func.distinct(Invoice.id)).label("invoice_count"),
            ).where(*non_draft_filters).where(Invoice.currency == chart_currency)
            yearly_stmt = _apply_amount_join(yearly_stmt).group_by(year_expr).order_by(year_expr.desc())
            yearly_rows = db.execute(yearly_stmt).all()
            max_year_total = max([int(row.total_cents or 0) for row in yearly_rows] or [0])
            yearly_revenue = [
                {
                    "year": int(row.year),
                    "total_cents": int(row.total_cents or 0),
                    "invoice_count": int(row.invoice_count or 0),
                    "bar_percent": 0 if max_year_total <= 0 else max(8, int(round((int(row.total_cents or 0) / max_year_total) * 100))),
                    "selected": int(row.year) == effective_year,
                }
                for row in yearly_rows
                if row.year is not None
            ]

            selected_year_filters = (*visible_filters, Invoice.issue_date.is_not(None), year_expr == effective_year)
            selected_non_draft_filters = (*selected_year_filters, Invoice.status != "draft")

            monthly_stmt = select(month_expr.label("month"), _amount_sum_expr().label("total_cents"))
            monthly_stmt = monthly_stmt.where(*selected_non_draft_filters).where(Invoice.currency == chart_currency)
            monthly_stmt = _apply_amount_join(monthly_stmt).group_by(month_expr)
            monthly_rows = {int(row.month): int(row.total_cents or 0) for row in db.execute(monthly_stmt).all() if row.month is not None}
            monthly_buckets = {idx: int(monthly_rows.get(idx, 0)) for idx in range(1, 13)}
            max_month_total = max(monthly_buckets.values() or [0])
            monthly_revenue = [
                {
                    "month": month,
                    "label": STATS_MONTH_LABELS[month - 1],
                    "total_cents": int(monthly_buckets[month]),
                    "bar_percent": 0 if max_month_total <= 0 else max(8, int(round((monthly_buckets[month] / max_month_total) * 100))),
                }
                for month in range(1, 13)
            ]

            totals_stmt = select(Invoice.currency.label("currency"), _amount_sum_expr().label("total_cents"))
            totals_stmt = totals_stmt.where(*selected_non_draft_filters).group_by(Invoice.currency).order_by(Invoice.currency.asc())
            totals_stmt = _apply_amount_join(totals_stmt)
            totals_by_currency = [
                (str(row.currency or "CZK"), int(row.total_cents or 0))
                for row in db.execute(totals_stmt).all()
            ]
            selected_year_total_cents = sum(total for currency, total in totals_by_currency if str(currency).upper() == chart_currency)
            selected_year_invoice_count = int(
                db.scalar(select(func.count(Invoice.id)).where(*selected_non_draft_filters)) or 0
            )
            foreign_currency_invoice_count = int(
                db.scalar(
                    select(func.count(Invoice.id))
                    .where(*selected_non_draft_filters)
                    .where(Invoice.currency != chart_currency)
                )
                or 0
            )
            invoices_by_status = [
                (str(row.status or "unknown"), int(row.count or 0))
                for row in db.execute(
                    select(Invoice.status.label("status"), func.count(Invoice.id).label("count"))
                    .where(*selected_year_filters)
                    .group_by(Invoice.status)
                    .order_by(Invoice.status.asc())
                ).all()
            ]

            current_month_start = _month_start(today)
            month_starts = [_add_months_to_month_start(current_month_start, offset) for offset in range(-11, 1)]
            window_start = month_starts[0]
            next_month_start = _add_months_to_month_start(current_month_start, 1)
            trailing_filters = (
                *visible_filters,
                Invoice.document_type == "invoice",
                ~Invoice.status.in_(["draft", "cancelled"]),
                Invoice.currency == chart_currency,
            )
            trailing_issue_stmt = select(
                year_expr.label("year"),
                month_expr.label("month"),
                _amount_sum_expr().label("invoiced_cents"),
                func.count(func.distinct(Invoice.id)).label("invoice_count"),
            ).where(*trailing_filters).where(Invoice.issue_date >= window_start).where(Invoice.issue_date < next_month_start)
            trailing_issue_stmt = _apply_amount_join(trailing_issue_stmt).group_by(year_expr, month_expr)
            issue_map = {
                (int(row.year), int(row.month)): (int(row.invoiced_cents or 0), int(row.invoice_count or 0))
                for row in db.execute(trailing_issue_stmt).all()
                if row.year is not None and row.month is not None
            }

            paid_year_expr = func.extract("year", Invoice.paid_on)
            paid_month_expr = func.extract("month", Invoice.paid_on)
            paid_stmt = select(
                paid_year_expr.label("year"),
                paid_month_expr.label("month"),
                _amount_sum_expr().label("paid_cents"),
                func.count(func.distinct(Invoice.id)).label("paid_count"),
            ).where(*trailing_filters).where(Invoice.status == "paid").where(Invoice.paid_on >= window_start).where(Invoice.paid_on < next_month_start)
            paid_stmt = _apply_amount_join(paid_stmt).group_by(paid_year_expr, paid_month_expr)
            paid_map = {
                (int(row.year), int(row.month)): (int(row.paid_cents or 0), int(row.paid_count or 0))
                for row in db.execute(paid_stmt).all()
                if row.year is not None and row.month is not None
            }
            max_rolling_value = 0
            rolling_rows = []
            total_invoiced_cents = 0
            total_paid_cents = 0
            total_invoice_count = 0
            total_paid_count = 0
            for month_start in month_starts:
                key = (int(month_start.year), int(month_start.month))
                invoiced_cents, invoice_count = issue_map.get(key, (0, 0))
                paid_cents, paid_count = paid_map.get(key, (0, 0))
                max_rolling_value = max(max_rolling_value, int(invoiced_cents), int(paid_cents))
                total_invoiced_cents += int(invoiced_cents)
                total_paid_cents += int(paid_cents)
                total_invoice_count += int(invoice_count)
                total_paid_count += int(paid_count)
                rolling_rows.append(
                    {
                        "month_start": month_start,
                        "label": f"{STATS_MONTH_LABELS[int(month_start.month) - 1]} {int(month_start.year)}",
                        "short_label": f"{STATS_MONTH_LABELS[int(month_start.month) - 1][:3]}. {str(int(month_start.year))[-2:]}",
                        "invoiced_cents": int(invoiced_cents),
                        "paid_cents": int(paid_cents),
                        "invoice_count": int(invoice_count),
                        "paid_count": int(paid_count),
                        "invoiced_bar_percent": 0,
                        "paid_bar_percent": 0,
                    }
                )
            if max_rolling_value > 0:
                for row in rolling_rows:
                    row["invoiced_bar_percent"] = max(8, int(round((int(row["invoiced_cents"]) / max_rolling_value) * 100))) if int(row["invoiced_cents"]) else 0
                    row["paid_bar_percent"] = max(8, int(round((int(row["paid_cents"]) / max_rolling_value) * 100))) if int(row["paid_cents"]) else 0

            trailing_twelve_months = {
                "window_start": window_start,
                "window_end": today,
                "rows": rolling_rows,
                "total_invoiced_cents": int(total_invoiced_cents),
                "total_paid_cents": int(total_paid_cents),
                "invoice_count": int(total_invoice_count),
                "paid_count": int(total_paid_count),
            }

            return {
                "selected_year": effective_year,
                "available_years": available_years,
                "chart_currency": chart_currency,
                "vat_view": vat_view,
                "vat_view_label": vat_view_label,
                "is_vat_payer": bool(is_vat_payer),
                "yearly_revenue": yearly_revenue,
                "monthly_revenue": monthly_revenue,
                "selected_year_total_cents": int(selected_year_total_cents),
                "selected_year_invoice_count": int(selected_year_invoice_count),
                "totals_by_currency": totals_by_currency,
                "invoices_by_status": invoices_by_status,
                "overdue_count": int(overdue_count),
                "invoices_count": int(invoices_count),
                "contacts_count": int(contacts_count),
                "foreign_currency_invoice_count": int(foreign_currency_invoice_count),
                "trailing_twelve_months": trailing_twelve_months,
            }

        def _recurring_plan_summary(plan: RecurringInvoicePlan | None) -> dict[str, object]:
            if plan is None:
                return {}
            template_invoice = getattr(plan, "template_invoice", None)
            template_number = str(getattr(template_invoice, "number", "") or "")
            if _is_internal_recurring_template_invoice(template_invoice):
                template_number = "Interní šablona"
            return {
                "id": int(getattr(plan, "id", 0) or 0),
                "name": str(getattr(plan, "name", "") or ""),
                "interval_unit": str(getattr(plan, "interval_unit", "") or "month"),
                "interval_count": int(getattr(plan, "interval_count", 1) or 1),
                "next_issue_date": getattr(plan, "next_issue_date", None),
                "due_in_days": int(getattr(plan, "due_in_days", 14) or 14),
                "is_active": bool(getattr(plan, "is_active", False)),
                "auto_issue": bool(getattr(plan, "auto_issue", False)),
                "auto_send": bool(getattr(plan, "auto_send", False)),
                "email_override": str(getattr(plan, "email_override", "") or ""),
                "template_invoice_id": int(getattr(plan, "template_invoice_id", 0) or 0),
                "template_number": template_number,
                "template_document_type": _normalize_invoice_document_type(getattr(template_invoice, "document_type", "invoice")),
                "template_contact_name": str(getattr(getattr(template_invoice, "contact", None), "name", "") or ""),
                "last_generated_invoice_id": int(getattr(plan, "last_generated_invoice_id", 0) or 0) or None,
                "template_is_internal": _is_internal_recurring_template_invoice(template_invoice),
            }

        def _process_recurring_plans(
            db: Session,
            *,
            request: Request,
            subject_id: int | None = None,
            plan_id: int | None = None,
            force: bool = False,
        ) -> dict[str, object]:
            today_local = date.today()
            stmt = (
                select(RecurringInvoicePlan)
                .options(selectinload(RecurringInvoicePlan.template_invoice).selectinload(Invoice.contact))
                .where(RecurringInvoicePlan.is_active == True)  # noqa: E712
            )
            if subject_id is not None:
                stmt = stmt.where(RecurringInvoicePlan.subject_id == int(subject_id))
            if plan_id is not None:
                stmt = stmt.where(RecurringInvoicePlan.id == int(plan_id))
            else:
                stmt = stmt.where(RecurringInvoicePlan.next_issue_date <= today_local)

            plans = db.scalars(stmt.order_by(RecurringInvoicePlan.next_issue_date.asc(), RecurringInvoicePlan.id.asc())).all()
            created_ids: list[int] = []
            sent_ids: list[int] = []
            errors: list[str] = []

            for plan in plans:
                template_invoice = getattr(plan, "template_invoice", None)
                contact = getattr(template_invoice, "contact", None) if template_invoice is not None else None
                subject = db.scalar(select(Subject).where(Subject.id == int(getattr(plan, "subject_id", 0) or 0)))
                if template_invoice is None or contact is None or subject is None:
                    errors.append(f"Plán {int(getattr(plan, 'id', 0) or 0)} nemá platnou šablonu.")
                    continue
                if _normalize_invoice_document_type(getattr(template_invoice, "document_type", "invoice")) == "credit_note":
                    errors.append(f"Plán {int(plan.id)} nemůže používat dobropis jako šablonu.")
                    continue
                run_date = today_local if force else getattr(plan, "next_issue_date", None)
                if not isinstance(run_date, date):
                    run_date = today_local
                if (not force) and run_date > today_local:
                    continue
                try:
                    source_items = db.scalars(
                        select(InvoiceItem)
                        .where(InvoiceItem.invoice_id == int(template_invoice.id))
                        .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                    ).all()
                    cloned_invoice = _clone_invoice_from_template(
                        db,
                        source_invoice=template_invoice,
                        source_items=list(source_items),
                        subject=subject,
                        issue_date=run_date,
                        due_date=run_date + timedelta(days=int(getattr(plan, "due_in_days", 14) or 14)),
                        document_type=str(getattr(template_invoice, "document_type", "invoice") or "invoice"),
                        source_invoice_id=int(template_invoice.id),
                        render_tokens=True,
                    )
                    _audit_log(
                        db,
                        action="invoice_created",
                        entity_type="invoice",
                        entity_id=int(cloned_invoice.id),
                        data={
                            "number": cloned_invoice.number,
                            "status": cloned_invoice.status,
                            "created_from_recurring_plan_id": int(plan.id),
                            "template_invoice_id": int(template_invoice.id),
                        },
                        subject_id=int(subject.id),
                    )
                    if bool(getattr(plan, "auto_issue", False)):
                        _issue_invoice_object(
                            db,
                            invoice=cloned_invoice,
                            subject=subject,
                            contact=contact,
                        )
                    if bool(getattr(plan, "auto_send", False)) and str(getattr(cloned_invoice, "status", "") or "") != "draft":
                        sent_ok, send_error = _send_invoice_email_automatically(
                            request,
                            db,
                            invoice=cloned_invoice,
                            subject=subject,
                            recipient_override=getattr(plan, "email_override", None),
                        )
                        if sent_ok:
                            sent_ids.append(int(cloned_invoice.id))
                        elif send_error:
                            errors.append(f"{plan.name}: {send_error}")
                    plan.last_run_at = utc_now()
                    plan.last_generated_invoice_id = int(cloned_invoice.id)
                    next_date = _add_recurrence_step(
                        run_date,
                        interval_unit=str(getattr(plan, "interval_unit", "month") or "month"),
                        interval_count=int(getattr(plan, "interval_count", 1) or 1),
                    )
                    while next_date <= today_local:
                        next_date = _add_recurrence_step(
                            next_date,
                            interval_unit=str(getattr(plan, "interval_unit", "month") or "month"),
                            interval_count=int(getattr(plan, "interval_count", 1) or 1),
                        )
                    plan.next_issue_date = next_date
                    db.commit()
                    created_ids.append(int(cloned_invoice.id))
                except Exception as exc:
                    db.rollback()
                    errors.append(f"{getattr(plan, 'name', 'Plán')}: {exc}")
                    continue

            return {
                "created_invoice_ids": created_ids,
                "sent_invoice_ids": sent_ids,
                "errors": errors,
            }

        def _build_home_vat_limit_context(db: Session, *, subject_id: int | None = None) -> dict[str, object]:
            current_year = date.today().year
            year_start = date(current_year, 1, 1)
            year_end = date(current_year, 12, 31)
            if subject_id is None:
                subject = _load_subject_for_current_session(db)
            else:
                subject = db.get(Subject, int(subject_id)) if int(subject_id or 0) > 0 else None
            empty_context: dict[str, object] = {
                "vat_limit": None,
                "flat_tax_limit": None,
                "recent_invoices": [],
                "yearly_revenue_overview": [],
                "chart_currency": "CZK",
                "top_clients": [],
                "outstanding_summary": {
                    "open_count": 0,
                    "overdue_count": 0,
                    "open_total_cents": 0,
                    "currency": "CZK",
                },
            }
            if subject is None:
                return empty_context

            sid = int(subject.id)
            is_vat_payer = bool(getattr(subject, "is_vat_payer", False))
            legal_form = _normalize_subject_legal_form(getattr(subject, "legal_form", "business"))
            uses_business_tax_limits = _subject_uses_business_tax_limits(legal_form)
            tax_regime = _normalize_tax_regime(getattr(subject, "tax_regime", "standard"))
            flat_tax_band = _normalize_flat_tax_band(getattr(subject, "flat_tax_band", "1"))
            flat_tax_income_profile = _normalize_flat_tax_income_profile(
                getattr(subject, "flat_tax_income_profile", "general")
            )
            chart_currency = _subject_chart_currency(subject)
            visible_invoice_filters = (
                Invoice.subject_id == sid,
                _invoice_visible_in_lists_clause(),
                Invoice.status != "draft",
            )
            recent_invoice_rows = db.scalars(
                select(Invoice)
                .options(selectinload(Invoice.contact))
                .where(*visible_invoice_filters)
                .order_by(*_invoice_newest_first_ordering())
                .limit(10)
            ).all()

            current_year_czk_row = db.execute(
                select(
                    func.coalesce(func.sum(Invoice.total_cents), 0),
                    func.count(Invoice.id),
                )
                .where(*visible_invoice_filters)
                .where(Invoice.currency == "CZK")
                .where(Invoice.issue_date >= year_start)
                .where(Invoice.issue_date <= year_end)
            ).one()
            turnover_czk_cents = int(current_year_czk_row[0] or 0)
            counted_invoice_count = int(current_year_czk_row[1] or 0)
            foreign_currency_invoice_count = int(
                db.scalar(
                    select(func.count(Invoice.id))
                    .where(*visible_invoice_filters)
                    .where(Invoice.currency != "CZK")
                    .where(Invoice.issue_date >= year_start)
                    .where(Invoice.issue_date <= year_end)
                )
                or 0
            )

            year_expr = func.extract("year", Invoice.issue_date)
            yearly_rows = db.execute(
                select(
                    year_expr.label("year"),
                    func.coalesce(func.sum(Invoice.total_cents), 0).label("total_cents"),
                    func.count(Invoice.id).label("invoice_count"),
                )
                .where(*visible_invoice_filters)
                .where(Invoice.currency == chart_currency)
                .where(Invoice.issue_date.is_not(None))
                .group_by(year_expr)
                .order_by(year_expr.desc())
                .limit(4)
            ).all()
            max_year_total = max([int(row.total_cents or 0) for row in yearly_rows] or [0])
            yearly_revenue_overview = [
                {
                    "year": int(row.year),
                    "total_cents": int(row.total_cents or 0),
                    "invoice_count": int(row.invoice_count or 0),
                    "bar_percent": 0 if max_year_total <= 0 else max(8, int(round((int(row.total_cents or 0) / max_year_total) * 100))),
                    "selected": int(row.year) == current_year,
                }
                for row in yearly_rows
                if row.year is not None
            ]

            thresholds: list[dict[str, object]] = []
            for title, amount_cents in VAT_REGISTRATION_THRESHOLDS:
                remaining_cents = max(int(amount_cents) - turnover_czk_cents, 0)
                exceeded_cents = max(turnover_czk_cents - int(amount_cents), 0)
                thresholds.append(
                    {
                        "title": title,
                        "amount_cents": int(amount_cents),
                        "remaining_cents": int(remaining_cents),
                        "exceeded_cents": int(exceeded_cents),
                        "reached": turnover_czk_cents >= int(amount_cents),
                    }
                )

            vat_limit: dict[str, object] | None = None
            if uses_business_tax_limits and not is_vat_payer:
                second_limit_cents = int(VAT_REGISTRATION_THRESHOLDS[-1][1])
                first_limit_cents = int(VAT_REGISTRATION_THRESHOLDS[0][1])
                status = "ok"
                badge_label = "Pod limitem"
                if turnover_czk_cents >= second_limit_cents:
                    status = "critical"
                    badge_label = "Nad 2. limitem"
                elif turnover_czk_cents >= first_limit_cents:
                    status = "warning"
                    badge_label = "Nad 1. limitem"

                note = (
                    "Od 1. 1. 2025 se v ČR pro povinnou registraci k DPH sleduje obrat za kalendářní rok. "
                    "Tady jde o orientační přehled podle vystavených faktur."
                )
                if foreign_currency_invoice_count:
                    note += " Faktury v jiné měně než CZK nejsou do součtu zahrnuté."
                vat_limit = {
                    "subject_name": str(getattr(subject, "name", "") or ""),
                    "year": current_year,
                    "turnover_czk_cents": turnover_czk_cents,
                    "counted_invoice_count": counted_invoice_count,
                    "foreign_currency_invoice_count": foreign_currency_invoice_count,
                    "thresholds": thresholds,
                    "status": status,
                    "badge_label": badge_label,
                    "note": note,
                }

            flat_tax_limit: dict[str, object] | None = None
            if uses_business_tax_limits and tax_regime == "flat":
                selected_limit_cents = _flat_tax_band_limit_cents(
                    band=flat_tax_band,
                    income_profile=flat_tax_income_profile,
                )
                all_flat_thresholds: list[dict[str, object]] = []
                for threshold in _flat_tax_thresholds_for_profile(income_profile=flat_tax_income_profile):
                    limit_cents = int(threshold["amount_cents"])
                    all_flat_thresholds.append(
                        {
                            **threshold,
                            "remaining_cents": max(limit_cents - turnover_czk_cents, 0),
                            "exceeded_cents": max(turnover_czk_cents - limit_cents, 0),
                            "reached": turnover_czk_cents >= limit_cents,
                            "selected": str(threshold["band"]) == flat_tax_band,
                        }
                    )
                final_flat_limit_cents = int(all_flat_thresholds[-1]["amount_cents"]) if all_flat_thresholds else 2_000_000 * 100
                flat_status = "ok"
                flat_badge_label = f"{flat_tax_band}. pásmo v limitu"
                if turnover_czk_cents > final_flat_limit_cents:
                    flat_status = "critical"
                    flat_badge_label = "Nad paušální režim"
                elif turnover_czk_cents > selected_limit_cents:
                    flat_status = "warning"
                    flat_badge_label = f"Nad {flat_tax_band}. pásmo"

                flat_note = (
                    "Orientační hlídání podle vystavených faktur v CZK za aktuální kalendářní rok. "
                    "Hranice paušálních pásem se od 1. 1. 2025 liší podle skladby příjmů; "
                    "počítáme je podle profilu zvoleného v nastavení."
                )
                if foreign_currency_invoice_count:
                    flat_note += " Faktury v jiné měně než CZK nejsou do součtu zahrnuté."
                flat_tax_limit = {
                    "subject_name": str(getattr(subject, "name", "") or ""),
                    "year": current_year,
                    "turnover_czk_cents": turnover_czk_cents,
                    "counted_invoice_count": counted_invoice_count,
                    "foreign_currency_invoice_count": foreign_currency_invoice_count,
                    "band": flat_tax_band,
                    "income_profile": flat_tax_income_profile,
                    "income_profile_note": FLAT_TAX_PROFILE_NOTES.get(flat_tax_income_profile, ""),
                    "selected_limit_cents": int(selected_limit_cents),
                    "thresholds": all_flat_thresholds,
                    "status": flat_status,
                    "badge_label": flat_badge_label,
                    "note": flat_note,
                }

            recent_invoices: list[dict[str, object]] = []
            for row in recent_invoice_rows:
                contact_name = ""
                try:
                    contact = getattr(row, "contact", None)
                    if contact is not None:
                        contact_name = str(getattr(contact, "name", "") or "")
                except Exception:
                    contact_name = ""
                recent_invoices.append(
                    {
                        "id": int(row.id),
                        "number": str(getattr(row, "number", "") or ""),
                        "status": str(getattr(row, "status", "") or ""),
                        "document_type": _normalize_invoice_document_type(getattr(row, "document_type", "invoice")),
                        "issue_date": getattr(row, "issue_date", None),
                        "due_date": getattr(row, "due_date", None),
                        "total_cents": int(getattr(row, "total_cents", 0) or 0),
                        "currency": str(getattr(row, "currency", "") or "CZK"),
                        "contact_name": contact_name,
                    }
                )

            buyer_name_expr = func.coalesce(Invoice.buyer_name_cache, "Bez kontaktu")
            top_client_rows = db.execute(
                select(
                    buyer_name_expr.label("name"),
                    Invoice.currency.label("currency"),
                    func.coalesce(func.sum(Invoice.total_cents), 0).label("total_cents"),
                    func.count(Invoice.id).label("invoice_count"),
                )
                .where(*visible_invoice_filters)
                .where(Invoice.issue_date >= year_start)
                .where(Invoice.issue_date <= year_end)
                .group_by(buyer_name_expr, Invoice.currency)
                .order_by(func.coalesce(func.sum(Invoice.total_cents), 0).desc(), func.count(Invoice.id).desc())
                .limit(5)
            ).all()
            top_clients = [
                {
                    "name": str(row.name or "Bez kontaktu"),
                    "currency": str(row.currency or "CZK"),
                    "total_cents": int(row.total_cents or 0),
                    "invoice_count": int(row.invoice_count or 0),
                }
                for row in top_client_rows
            ]

            outstanding_row = db.execute(
                select(
                    func.count(Invoice.id).label("open_count"),
                    func.coalesce(func.sum(case((Invoice.currency == chart_currency, Invoice.total_cents), else_=0)), 0).label("open_total_cents"),
                    func.coalesce(func.sum(case((Invoice.due_date < date.today(), 1), else_=0)), 0).label("overdue_count"),
                )
                .where(Invoice.subject_id == sid)
                .where(_invoice_visible_in_lists_clause())
                .where(Invoice.status.in_(["issued", "sent"]))
            ).one()

            return {
                "vat_limit": vat_limit,
                "flat_tax_limit": flat_tax_limit,
                "recent_invoices": recent_invoices,
                "yearly_revenue_overview": yearly_revenue_overview,
                "chart_currency": chart_currency,
                "top_clients": top_clients,
                "outstanding_summary": {
                    "open_count": int(outstanding_row.open_count or 0),
                    "overdue_count": int(outstanding_row.overdue_count or 0),
                    "open_total_cents": int(outstanding_row.open_total_cents or 0),
                    "currency": chart_currency,
                },
            }

        def _maybe_send_tax_limit_alerts(
            db: Session,
            *,
            request: Request,
            vat_limit: dict[str, object] | None,
            flat_tax_limit: dict[str, object] | None,
            subject_id: int | None = None,
            recipient_email: str | None = None,
        ) -> None:
            if subject_id is None:
                subject = _load_subject_for_current_session(db)
            else:
                subject = db.get(Subject, int(subject_id)) if int(subject_id or 0) > 0 else None
            if subject is None or not bool(getattr(subject, "tax_alerts_enabled", False)):
                return

            recipient = str(getattr(subject, "tax_alert_email", "") or "").strip()
            if not recipient and recipient_email:
                recipient = str(recipient_email or "").strip()
            if not recipient and request is not None:
                current_user = _current_user_settings_view(db, request)
                recipient = str(current_user.get("email") or "").strip()
            if not recipient:
                recipient = str(getattr(subject, "email", "") or "").strip()
            if not looks_like_email(recipient):
                return

            subject_email = str(getattr(subject, "email", "") or "").strip()
            subject_name = str(getattr(subject, "name", "") or "").strip()
            from_email = (settings.smtp_from_email or subject_email or settings.issuer_email or "").strip()
            from_name = (settings.smtp_from_name or subject_name or settings.issuer_name or "").strip()

            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )
            if not smtp_is_configured(smtp_cfg) or not looks_like_email(from_email):
                return

            current_year = date.today().year
            pending_alerts: list[dict[str, object]] = []

            if vat_limit is not None:
                first_limit_cents = int(VAT_REGISTRATION_THRESHOLDS[0][1])
                second_limit_cents = int(VAT_REGISTRATION_THRESHOLDS[-1][1])
                turnover_cents = int(vat_limit.get("turnover_czk_cents") or 0)
                vat_stage, vat_stage_label = _tax_alert_stage_for_turnover(
                    turnover_cents=turnover_cents,
                    limit_cents=first_limit_cents,
                )
                if turnover_cents >= second_limit_cents:
                    vat_stage = 4
                    vat_stage_label = "nad druhým limitem"
                last_stage = int(getattr(subject, "vat_alert_last_stage", 0) or 0)
                last_year = getattr(subject, "vat_alert_last_year", None)
                if int(last_year or 0) != current_year:
                    last_stage = 0
                if vat_stage > last_stage:
                    pending_alerts.append(
                        {
                            "kind": "vat",
                            "stage": vat_stage,
                            "stage_label": vat_stage_label,
                            "title": "Limit DPH",
                            "turnover_cents": turnover_cents,
                            "limit_cents": first_limit_cents,
                            "extra_line": (
                                f"Druhý limit: {format_cents(second_limit_cents, 'CZK')}."
                                if vat_stage >= 4
                                else f"První sledovaný limit: {format_cents(first_limit_cents, 'CZK')}."
                            ),
                        }
                    )

            if flat_tax_limit is not None:
                turnover_cents = int(flat_tax_limit.get("turnover_czk_cents") or 0)
                selected_limit_cents = int(flat_tax_limit.get("selected_limit_cents") or 0)
                flat_stage, flat_stage_label = _tax_alert_stage_for_turnover(
                    turnover_cents=turnover_cents,
                    limit_cents=selected_limit_cents,
                )
                last_stage = int(getattr(subject, "flat_tax_alert_last_stage", 0) or 0)
                last_year = getattr(subject, "flat_tax_alert_last_year", None)
                if int(last_year or 0) != current_year:
                    last_stage = 0
                if flat_stage > last_stage:
                    pending_alerts.append(
                        {
                            "kind": "flat_tax",
                            "stage": flat_stage,
                            "stage_label": flat_stage_label,
                            "title": f"Paušální daň ({flat_tax_limit.get('band')}. pásmo)",
                            "turnover_cents": turnover_cents,
                            "limit_cents": selected_limit_cents,
                            "extra_line": (
                                f"Profil příjmů: {flat_tax_limit.get('income_profile_note') or '—'}."
                            ),
                        }
                    )

            if not pending_alerts:
                return

            dashboard_url = str(request.base_url).rstrip("/") + "/"
            subject_line = (
                f"Upozornění na daňový limit: {subject_name or 'Fakturek'}"
                if len(pending_alerts) > 1
                else f"Upozornění: {pending_alerts[0]['title']} pro {subject_name or 'Fakturek'}"
            )
            body_parts = [
                "Dobrý den,",
                "",
                f"u subjektu {subject_name or 'aktuální subjekt'} došlo k posunu v hlídání daňových limitů.",
                "",
            ]
            for alert in pending_alerts:
                body_parts.extend(
                    [
                        f"{alert['title']}: {alert['stage_label']}.",
                        f"Aktuální obrat: {format_cents(int(alert['turnover_cents'] or 0), 'CZK')}.",
                        f"Sledovaný limit: {format_cents(int(alert['limit_cents'] or 0), 'CZK')}.",
                        str(alert.get("extra_line") or ""),
                        "",
                    ]
                )
            body_parts.extend(
                [
                    "Kontrola je orientační a vychází z vystavených faktur v CZK za aktuální kalendářní rok.",
                    f"Přehled otevřeš tady: {dashboard_url}",
                ]
            )
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=[recipient],
                subject=subject_line,
                body="\n".join(line for line in body_parts if line is not None),
            )

            try:
                message_id, _debug = send_via_smtp(smtp_cfg, msg)
            except Exception:
                db.rollback()
                return

            if any(alert["kind"] == "vat" for alert in pending_alerts):
                vat_alert = next(alert for alert in pending_alerts if alert["kind"] == "vat")
                subject.vat_alert_last_stage = int(vat_alert["stage"])
                subject.vat_alert_last_year = current_year
            if any(alert["kind"] == "flat_tax" for alert in pending_alerts):
                flat_alert = next(alert for alert in pending_alerts if alert["kind"] == "flat_tax")
                subject.flat_tax_alert_last_stage = int(flat_alert["stage"])
                subject.flat_tax_alert_last_year = current_year
            db.add(subject)
            _audit_log(
                db,
                request=request,
                action="subject_tax_alert_sent",
                entity_type="subject",
                entity_id=int(subject.id),
                data={
                    "recipient": recipient,
                    "message_id": message_id,
                    "alerts": [
                        {
                            "kind": str(alert["kind"]),
                            "stage": int(alert["stage"]),
                            "title": str(alert["title"]),
                        }
                        for alert in pending_alerts
                    ],
                },
                subject_id=int(subject.id),
            )
            try:
                db.commit()
            except SQLAlchemyError:
                db.rollback()

        @app.post("/internal/jobs/tax-alerts")
        def tax_alerts_job_run(request: Request):
            try:
                _verify_internal_job_request(request)
            except HTTPException as exc:
                return JSONResponse(status_code=int(exc.status_code), content={"detail": str(exc.detail)})
            if not _db_enabled:
                return JSONResponse(status_code=503, content={"detail": "Database unavailable"})
            try:
                from fakturek.db import get_sessionmaker  # type: ignore

                SessionLocal = get_sessionmaker()
                processed_subject_ids: list[int] = []
                with SessionLocal() as db:  # type: ignore
                    subjects = db.scalars(
                        select(Subject)
                        .where(Subject.tax_alerts_enabled == True)  # noqa: E712
                        .order_by(Subject.id.asc())
                    ).all()
                    for subject in subjects:
                        sid = int(getattr(subject, "id", 0) or 0)
                        if sid <= 0:
                            continue
                        context = _build_home_vat_limit_context(db, subject_id=sid)
                        _maybe_send_tax_limit_alerts(
                            db,
                            request=request,
                            vat_limit=context.get("vat_limit") if isinstance(context.get("vat_limit"), dict) else None,
                            flat_tax_limit=context.get("flat_tax_limit") if isinstance(context.get("flat_tax_limit"), dict) else None,
                            subject_id=sid,
                        )
                        processed_subject_ids.append(sid)
                return {"status": "ok", "processed_subject_ids": processed_subject_ids}
            except Exception:
                logging.getLogger("fakturek").exception("Tax alerts job failed")
                return JSONResponse(status_code=500, content={"detail": "Internal job failed"})

        def _user_subject_link(db: Session, *, user_id: int | None, subject_id: int | None) -> UserSubject | None:
            if user_id is None or subject_id is None:
                return None
            try:
                return db.scalar(
                    select(UserSubject)
                    .where(UserSubject.user_id == int(user_id))
                    .where(UserSubject.subject_id == int(subject_id))
                )
            except SQLAlchemyError:
                return None

        def _subject_role_value(link: UserSubject | None) -> str:
            if link is None:
                return ""
            return str(getattr(link, "role", "") or "").strip().lower()

        def _user_can_view_subject(db: Session, *, user_id: int | None, subject_id: int | None) -> bool:
            link = _user_subject_link(db, user_id=user_id, subject_id=subject_id)
            return bool(link is not None and bool(getattr(link, "can_view", False)))

        def _user_can_export_subject(db: Session, *, user_id: int | None, subject_id: int | None) -> bool:
            link = _user_subject_link(db, user_id=user_id, subject_id=subject_id)
            if link is None:
                return False
            role_value = _subject_role_value(link)
            if role_value == "owner":
                return True
            return bool(getattr(link, "can_export", False))


        def _current_request_can_export_subject(
            db: Session,
            *,
            request: Request,
            subject_id: int | None,
        ) -> bool:
            if not settings.auth_required:
                return True
            return _user_can_export_subject(
                db,
                user_id=_current_user_id_or_none(request),
                subject_id=subject_id,
            )

























        def _user_can_manage_subject(db: Session, *, user_id: int | None, subject_id: int | None) -> bool:
            link = _user_subject_link(db, user_id=user_id, subject_id=subject_id)
            return _subject_role_value(link) == "owner"

        def _user_can_manage_subject_users(db: Session, *, user_id: int | None, subject_id: int | None) -> bool:
            link = _user_subject_link(db, user_id=user_id, subject_id=subject_id)
            if link is None:
                return False
            role_value = _subject_role_value(link)
            if role_value == "owner":
                return True
            return role_value == "manager"

        def _subject_user_role_options(db: Session, *, user_id: int | None, subject_id: int | None) -> list[str]:
            if _user_can_manage_subject(db, user_id=user_id, subject_id=subject_id):
                return ["owner", "manager", "accountant", "viewer", "user"]
            if _user_can_manage_subject_users(db, user_id=user_id, subject_id=subject_id):
                return ["manager", "accountant", "viewer", "user"]
            return []

        def _normalize_subject_access_flags(
            *,
            role: str | None,
            can_view: bool,
            can_edit: bool,
            can_issue: bool,
            can_export: bool,
        ) -> tuple[str, bool, bool, bool, bool]:
            normalized_role = (str(role or "user").strip().lower() or "user")
            view_flag = bool(can_view)
            edit_flag = bool(can_edit)
            issue_flag = bool(can_issue)
            export_flag = bool(can_export)
            if normalized_role == "owner":
                return "owner", True, True, True, True
            if normalized_role == "manager":
                return "manager", True, True, True, True
            if normalized_role == "accountant":
                return "accountant", True, edit_flag, False, True
            if normalized_role == "viewer":
                return "viewer", True, False, False, False
            if issue_flag:
                edit_flag = True
            if edit_flag or issue_flag or export_flag:
                view_flag = True
            return normalized_role, view_flag, edit_flag, issue_flag, export_flag

        def _subject_owner_count(db: Session, *, subject_id: int | None) -> int:
            if subject_id is None:
                return 0
            try:
                return int(
                    db.scalar(
                        select(func.count(UserSubject.id))
                        .where(UserSubject.subject_id == int(subject_id))
                        .where(UserSubject.role == "owner")
                    )
                    or 0
                )
            except SQLAlchemyError:
                return 0

        def _user_can_manage_subject_link(
            db: Session,
            *,
            user_id: int | None,
            subject_id: int | None,
            link: UserSubject | None,
        ) -> bool:
            if link is None:
                return False
            actor_link = _user_subject_link(db, user_id=user_id, subject_id=subject_id)
            if actor_link is None:
                return False
            actor_role = _subject_role_value(actor_link)
            if actor_role == "owner":
                return True
            if actor_role != "manager":
                return False
            return _subject_role_value(link) != "owner"

        def _find_user_by_identifier(db: Session, *, identifier: str) -> User | None:
            ident = (str(identifier or "") or "").strip()
            if not ident:
                return None
            try:
                return db.scalar(
                    select(User).where(or_(User.username == ident, User.email == ident)).limit(1)
                )
            except SQLAlchemyError:
                return None

        def _first_viewable_subject_id(
            db: Session,
            *,
            user_id: int | None,
            exclude_subject_id: int | None = None,
        ) -> int | None:
            if user_id is None:
                return None
            try:
                links = db.scalars(
                    select(UserSubject)
                    .where(UserSubject.user_id == int(user_id))
                    .order_by(UserSubject.subject_id.asc(), UserSubject.id.asc())
                ).all()
            except SQLAlchemyError:
                return None
            for link in links:
                sid = int(link.subject_id)
                if exclude_subject_id is not None and sid == int(exclude_subject_id):
                    continue
                if bool(getattr(link, "can_view", False)):
                    return sid
            return None

        def _refresh_current_session_access_context(
            request: Request,
            db: Session,
            *,
            preferred_subject_id: int | None = None,
        ) -> None:
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return
            current_sid: int | None
            try:
                raw_sid = request.session.get("subject_id")
                current_sid = int(raw_sid) if raw_sid is not None else None
            except Exception:
                current_sid = None
            if current_sid is not None and _user_can_view_subject(db, user_id=user_id, subject_id=current_sid):
                return
            if preferred_subject_id is not None and _user_can_view_subject(
                db,
                user_id=user_id,
                subject_id=int(preferred_subject_id),
            ):
                request.session["subject_id"] = int(preferred_subject_id)
                return
            replacement_sid = _first_viewable_subject_id(db, user_id=user_id)
            if replacement_sid is not None:
                request.session["subject_id"] = int(replacement_sid)

        def _subject_access_post_save_target(
            request: Request,
            db: Session,
            *,
            next_url: str | None,
            subject_id: int,
        ) -> str:
            fallback_settings = "/settings#subjects-admin"
            target = _safe_next_url(next_url, fallback_settings)
            return _with_saved_flag(target, fallback=fallback_settings)

        def _bank_accounts_view_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            def _format_sync_dt(value: datetime | None) -> str:
                if value is None:
                    return ""
                try:
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=ZoneInfo("UTC"))
                    return value.astimezone(ZoneInfo("Europe/Prague")).strftime("%d.%m.%Y %H:%M")
                except Exception:
                    return ""

            return [
                {
                    **{
                        "id": int(acc.id),
                        "label": str(acc.label or ""),
                        "account_number": "" if str(acc.country or "").upper() == "SK" else str(acc.account_number or ""),
                        "display": format_iban_for_display(acc.iban) if str(acc.country or "").upper() == "SK" and getattr(acc, "iban", None) else (str(acc.account_number or "") or format_iban_for_display(acc.iban)),
                        "iban": format_iban_for_display(acc.iban) if getattr(acc, "iban", None) else "",
                        "bic": str(acc.bic or ""),
                        "country": str(acc.country or ""),
                        "currency": str(getattr(acc, "currency", "") or "CZK"),
                        "is_default": bool(acc.is_default),
                        "payment_sync_provider": _normalize_payment_sync_provider(getattr(acc, "payment_sync_provider", None)),
                        "payment_sync_provider_label": _payment_sync_provider_label(getattr(acc, "payment_sync_provider", None)),
                        "payment_sync_enabled": bool(getattr(acc, "payment_sync_enabled", False)),
                        "payment_sync_auto_pair": bool(getattr(acc, "payment_sync_auto_pair", True)),
                        "payment_sync_email_parser": _normalize_payment_sync_email_parser(getattr(acc, "payment_sync_email_parser", None)),
                        "payment_sync_alert_localpart": str(getattr(acc, "payment_sync_alert_localpart", "") or "").strip(),
                        "payment_sync_alert_email": _payment_sync_alert_email_for_account(acc),
                        "payment_sync_last_email_uid": str(getattr(acc, "payment_sync_last_email_uid", "") or "").strip(),
                        "payment_sync_last_checked_at": _format_sync_dt(getattr(acc, "payment_sync_last_checked_at", None)),
                        "payment_sync_last_success_at": _format_sync_dt(getattr(acc, "payment_sync_last_success_at", None)),
                        "payment_sync_last_error": safe_bank_sync_error_message(
                            getattr(acc, "payment_sync_last_error", "")
                        ),
                        "has_fio_api_token": bool(str(getattr(acc, "fio_api_token", "") or "").strip()),
                        "invoice_count": int(
                            db.scalar(select(func.count(Invoice.id)).where(Invoice.bank_account_id == int(acc.id))) or 0
                        ),
                        "imported_transaction_count": int(
                            db.scalar(
                                select(func.count(BankTransaction.id)).where(
                                    BankTransaction.subject_bank_account_id == int(acc.id)
                                )
                            )
                            or 0
                        ),
                        "stored_email_count": int(
                            db.scalar(
                                select(func.count(BankIncomingEmail.id)).where(
                                    BankIncomingEmail.subject_bank_account_id == int(acc.id)
                                )
                            )
                            or 0
                        ),
                    },
                    **{
                        "payment_sync_email_parser_label": _payment_sync_email_parser_label(getattr(acc, "payment_sync_email_parser", None)),
                        "payment_sync_email_sender_filter": _payment_sync_email_defaults(getattr(acc, "payment_sync_email_parser", None)).get("sender")
                        or str(getattr(acc, "payment_sync_email_sender_filter", "") or "").strip(),
                        "payment_sync_email_subject_filter": _payment_sync_email_defaults(getattr(acc, "payment_sync_email_parser", None)).get("subject")
                        or str(getattr(acc, "payment_sync_email_subject_filter", "") or "").strip(),
                        "payment_sync_email_parser_description": _payment_sync_email_defaults(getattr(acc, "payment_sync_email_parser", None)).get("description"),
                    },
                    "payment_sync_status": (
                        "Aktivní"
                        if bool(getattr(acc, "payment_sync_enabled", False))
                        and _normalize_payment_sync_provider(getattr(acc, "payment_sync_provider", None)) == "fio_api"
                        and bool(str(getattr(acc, "fio_api_token", "") or "").strip())
                        else (
                            "Aktivní"
                            if bool(getattr(acc, "payment_sync_enabled", False))
                            and _normalize_payment_sync_provider(getattr(acc, "payment_sync_provider", None)) == "email_bank"
                            and _normalize_payment_sync_email_parser(getattr(acc, "payment_sync_email_parser", None)) != "pending"
                            else ("Připraveno" if bool(getattr(acc, "payment_sync_enabled", False)) else "Vypnuto")
                        )
                    ),
                }
                for acc in _list_subject_bank_accounts(db, subject_id=int(subject_id))
            ]

        def _blank_account_prefill(
            *,
            country: str | None,
            currency: str | None = None,
            is_default: bool = False,
        ) -> dict[str, object]:
            return {
                "id": "",
                "label": "",
                "account_number": "",
                "iban": "",
                "bic": "",
                "country": (str(country or "CZ") or "CZ").upper(),
                "currency": (str(currency or "CZK") or "CZK").upper(),
                "is_default": bool(is_default),
                "payment_sync_provider": "none",
                "payment_sync_enabled": False,
                "payment_sync_auto_pair": True,
                "fio_api_token": "",
                "has_fio_api_token": False,
                "payment_sync_email_sender_filter": "",
                "payment_sync_email_subject_filter": "",
                "payment_sync_email_parser": "pending",
                "payment_sync_email_parser_description": "",
                "payment_sync_alert_localpart": "",
                "payment_sync_alert_email": "",
                "payment_sync_last_email_uid": "",
            }

        def _account_prefill_from_row(account: SubjectBankAccount | None, *, fallback_country: str | None = None) -> dict[str, object]:
            if account is None:
                return _blank_account_prefill(country=fallback_country)
            defaults = _payment_sync_email_defaults(getattr(account, "payment_sync_email_parser", None))
            return {
                "id": int(account.id),
                "label": str(account.label or ""),
                "account_number": "" if str(account.country or fallback_country or "CZ").upper() == "SK" else str(account.account_number or ""),
                "iban": format_iban_for_display(account.iban) if getattr(account, "iban", None) else "",
                "bic": str(account.bic or ""),
                "country": str(account.country or fallback_country or "CZ"),
                "currency": str(getattr(account, "currency", None) or "CZK"),
                "is_default": bool(account.is_default),
                "payment_sync_provider": _normalize_payment_sync_provider(getattr(account, "payment_sync_provider", None)),
                "payment_sync_enabled": bool(getattr(account, "payment_sync_enabled", False)),
                "payment_sync_auto_pair": bool(getattr(account, "payment_sync_auto_pair", True)),
                "fio_api_token": "",
                "has_fio_api_token": bool(str(getattr(account, "fio_api_token", "") or "").strip()),
                "payment_sync_email_sender_filter": defaults.get("sender")
                or str(getattr(account, "payment_sync_email_sender_filter", "") or "").strip(),
                "payment_sync_email_subject_filter": defaults.get("subject")
                or str(getattr(account, "payment_sync_email_subject_filter", "") or "").strip(),
                "payment_sync_email_parser": _normalize_payment_sync_email_parser(getattr(account, "payment_sync_email_parser", None)),
                "payment_sync_email_parser_description": defaults.get("description") or "",
                "payment_sync_alert_localpart": str(getattr(account, "payment_sync_alert_localpart", "") or "").strip(),
                "payment_sync_alert_email": _payment_sync_alert_email_for_account(account),
                "payment_sync_last_email_uid": str(getattr(account, "payment_sync_last_email_uid", "") or "").strip(),
            }

        def _account_prefill_from_form(
            form,
            *,
            default_country: str,
            default_currency: str,
            is_default: bool = False,
            account_id: int | None = None,
            has_existing_fio_token: bool = False,
        ) -> dict[str, object]:
            sync_provider = _normalize_payment_sync_provider(form.get("payment_sync_provider"))
            parser_name = _normalize_payment_sync_email_parser(form.get("payment_sync_email_parser"))
            defaults = _payment_sync_email_defaults(parser_name)
            return {
                "id": int(account_id) if account_id is not None else "",
                "label": (form.get("label") or "").strip(),
                "account_number": (form.get("account_number") or "").strip(),
                "iban": (form.get("iban") or "").strip(),
                "bic": (form.get("bic") or "").strip(),
                "country": ((form.get("country") or default_country or "CZ").strip().upper() or "CZ"),
                "currency": ((form.get("currency") or default_currency or "CZK").strip().upper() or "CZK"),
                "is_default": bool(form.get("is_default")) if "is_default" in form else bool(is_default),
                "payment_sync_provider": sync_provider,
                "payment_sync_enabled": sync_provider != "none",
                "payment_sync_auto_pair": sync_provider != "none",
                "fio_api_token": (form.get("fio_api_token") or "").strip(),
                "has_fio_api_token": bool(has_existing_fio_token),
                "payment_sync_email_sender_filter": str(defaults.get("sender") or "").strip().lower(),
                "payment_sync_email_subject_filter": str(defaults.get("subject") or "").strip(),
                "payment_sync_email_parser": parser_name,
                "payment_sync_email_parser_description": str(defaults.get("description") or "").strip(),
                "payment_sync_alert_localpart": "",
                "payment_sync_alert_email": "",
                "payment_sync_last_email_uid": "",
            }

        def _decode_fio_api_token(value: str | None) -> str:
            return str(
                decrypt_secret(
                    value,
                    secret_key=str(settings.data_encryption_key or ""),
                    purpose="fio-api-token",
                )
                or ""
            ).strip()

        def _encode_fio_api_token(value: str | None) -> str | None:
            return encrypt_secret(
                value,
                secret_key=str(settings.data_encryption_key or ""),
                purpose="fio-api-token",
            )

        def _test_fio_api_token(value: str | None) -> str | None:
            token = str(value or "").strip()
            if not token:
                return "Pro Fio API vyplň token bankovnictví."
            today_value = date.today()
            try:
                fetch_fio_transactions(
                    token,
                    date_from=today_value,
                    date_to=today_value,
                    base_url=str(getattr(settings, "fio_api_base_url", "") or ""),
                    timeout_seconds=min(float(getattr(settings, "fio_timeout_seconds", 30.0) or 30.0), 30.0),
                )
            except BankSyncError as exc:
                return f"Fio API token se nepodařilo ověřit: {exc}"
            return None

        def _payment_sync_date_window(account: SubjectBankAccount, *, today_local: date | None = None) -> tuple[date, date]:
            today_value = today_local or date.today()
            cursor_date = getattr(account, "payment_sync_cursor_date", None)
            if cursor_date is None:
                return today_value, today_value
            return max(cursor_date - timedelta(days=BANK_SYNC_OVERLAP_DAYS), today_value - timedelta(days=365)), today_value

        def _refresh_payment_sync_checkpoints(
            account: SubjectBankAccount,
            *,
            previous_provider: str | None,
            previous_enabled: bool,
            previous_email_parser: str | None,
            previous_fio_token: str | None,
            effective_fio_token: str | None,
        ) -> None:
            current_provider = _normalize_payment_sync_provider(getattr(account, "payment_sync_provider", None))
            current_enabled = bool(getattr(account, "payment_sync_enabled", False))
            current_email_parser = _normalize_payment_sync_email_parser(getattr(account, "payment_sync_email_parser", None))

            if not current_enabled or current_provider == "none":
                return

            if current_provider == "fio_api":
                token_changed = bool((effective_fio_token or "").strip()) and (effective_fio_token or "") != (previous_fio_token or "")
                if (
                    not previous_enabled
                    or _normalize_payment_sync_provider(previous_provider) != "fio_api"
                    or token_changed
                    or getattr(account, "payment_sync_cursor_date", None) is None
                ):
                    account.payment_sync_cursor_date = date.today()
                    account.payment_sync_last_error = None
                return

            if current_provider == "email_bank":
                parser_changed = current_email_parser != _normalize_payment_sync_email_parser(previous_email_parser)
                if (
                    not previous_enabled
                    or _normalize_payment_sync_provider(previous_provider) != "email_bank"
                    or parser_changed
                ):
                    account.payment_sync_last_email_uid = None
                    account.payment_sync_last_success_at = None
                    account.payment_sync_last_error = None

        def _bank_transaction_variable_symbol_candidates(*values: object | None) -> list[str]:
            candidates: list[str] = []
            for value in values:
                text = normalize_spaces(str(value or ""))
                if not text:
                    continue
                raw_values = [text]
                raw_values.extend(re.findall(r"(?:^|[\s,/;])VS[:\s/.-]*(\d{1,10})\b", text, flags=re.IGNORECASE))
                raw_values.extend(re.findall(r"\bvariabiln[ií]\s+symbol[:\s/.-]*(\d{1,10})\b", text, flags=re.IGNORECASE))
                for raw in raw_values:
                    normalized = digits_only(raw)[:10]
                    if normalized and normalized not in candidates:
                        candidates.append(normalized)
            return candidates

        def _bank_sync_candidate_invoices(
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
            variable_symbols = _bank_transaction_variable_symbol_candidates(variable_symbol, message)
            if amount_cents <= 0:
                variable_symbols = []
            invoices = db.scalars(
                select(Invoice)
                .options(selectinload(Invoice.contact))
                .where(Invoice.subject_id == int(subject_id))
                .where(Invoice.status.in_(["issued", "sent"]))
                .where(Invoice.document_type.in_(["invoice", "proforma"]))
                .where(Invoice.issue_date <= booked_on)
                .where(Invoice.total_cents == int(amount_cents))
                .where(func.upper(Invoice.currency) == str(currency or "CZK").upper())
                .where(or_(Invoice.bank_account_id == int(account_id), Invoice.bank_account_id.is_(None)))
                .order_by(*_invoice_newest_first_ordering())
            ).all()
            matched: list[Invoice] = []
            if variable_symbols:
                for invoice in invoices:
                    if _invoice_variable_symbol(invoice, contact=invoice.contact) in variable_symbols:
                        matched.append(invoice)
                if matched:
                    return matched
            note = normalize_spaces(message)
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

        def _match_bank_transaction_row(
            db: Session,
            *,
            account: SubjectBankAccount,
            row: BankTransaction,
            request: Request | None = None,
        ) -> bool:
            if row.matched_invoice_id is not None:
                return False
            if str(getattr(row, "direction", "") or "").strip().lower() != "incoming":
                return False
            if int(getattr(row, "amount_cents", 0) or 0) <= 0:
                return False
            candidates = _bank_sync_candidate_invoices(
                db,
                subject_id=int(account.subject_id),
                account_id=int(account.id),
                booked_on=row.booked_on,
                amount_cents=int(row.amount_cents or 0),
                currency=str(row.currency or "CZK"),
                variable_symbol=row.variable_symbol,
                message=getattr(row, "message", None),
            )
            if len(candidates) != 1:
                return False
            transaction = ImportedBankTransaction(
                provider=str(row.provider or "fio_api"),
                external_id=str(row.external_id or ""),
                booked_on=row.booked_on,
                amount_cents=int(row.amount_cents or 0),
                currency=str(row.currency or "CZK"),
                direction=str(row.direction or "incoming"),
                variable_symbol=row.variable_symbol,
                constant_symbol=row.constant_symbol,
                specific_symbol=row.specific_symbol,
                counterparty_account=row.counterparty_account,
                counterparty_name=row.counterparty_name,
                message=row.message,
                raw_payload={},
            )
            payment = _apply_bank_transaction_match(
                db,
                invoice=candidates[0],
                transaction=transaction,
                request=request,
            )
            row.matched_invoice_id = int(candidates[0].id)
            row.payment_id = int(payment.id)
            row.matched_at = utc_now()
            db.add(row)
            return True

        def _retry_existing_unmatched_bank_transactions(
            db: Session,
            *,
            account: SubjectBankAccount,
            request: Request | None = None,
        ) -> int:
            matched_count = 0
            existing_unmatched_rows = db.scalars(
                select(BankTransaction)
                .where(BankTransaction.subject_bank_account_id == int(account.id))
                .where(BankTransaction.matched_invoice_id.is_(None))
                .where(BankTransaction.direction == "incoming")
                .where(BankTransaction.amount_cents > 0)
                .order_by(BankTransaction.booked_on.desc(), BankTransaction.id.desc())
            ).all()
            for row in existing_unmatched_rows:
                if _match_bank_transaction_row(
                    db,
                    account=account,
                    row=row,
                    request=request,
                ):
                    matched_count += 1
            return matched_count

        def _apply_bank_transaction_match(
            db: Session,
            *,
            invoice: Invoice,
            transaction: ImportedBankTransaction,
            request: Request | None = None,
        ) -> Payment:
            old_status = str(getattr(invoice, "status", "") or "").strip().lower()
            changed, error = _apply_invoice_status_transition(
                invoice,
                new_status="paid",
                paid_on=transaction.booked_on,
            )
            if not changed and old_status != "paid":
                raise BankSyncError(error or "Nepodařilo se označit fakturu jako zaplacenou.")

            payment_provider_label = (
                "Fio API"
                if transaction.provider == "fio_api"
                else (
                        "e-mail Raiffeisenbank"
                        if transaction.provider == "email_bank_raiffeisenbank_cz"
                        else (
                            "e-mail České spořitelny"
                            if transaction.provider == "email_bank_csas_cz"
                            else (
                        "e-mail ČSOB"
                        if transaction.provider == "email_bank_csob_cz"
                        else ("e-mail Fio banky" if transaction.provider == "email_bank_fio_email_cz" else "bankovní sync")
                            )
                    )
                )
            )
            payment = Payment(
                invoice_id=int(invoice.id),
                paid_on=transaction.booked_on,
                amount_cents=int(transaction.amount_cents),
                note=f"Automatické párování {payment_provider_label} ({transaction.external_id})",
            )
            invoice.paid_on = transaction.booked_on
            db.add(payment)
            db.flush()
            _audit_log(
                db,
                request=request,
                action="invoice_paid_bank_sync",
                entity_type="invoice",
                entity_id=int(invoice.id),
                data={
                    "provider": transaction.provider,
                    "external_id": transaction.external_id,
                    "amount_cents": int(transaction.amount_cents),
                    "currency": transaction.currency,
                    "variable_symbol": transaction.variable_symbol,
                    "booked_on": str(transaction.booked_on),
                    "counterparty_account": transaction.counterparty_account,
                    "counterparty_name": transaction.counterparty_name,
                    "message": transaction.message,
                    "from": old_status,
                    "to": "paid",
                },
                subject_id=int(invoice.subject_id),
            )
            return payment

        def _import_bank_transaction_row(
            db: Session,
            *,
            account: SubjectBankAccount,
            imported: ImportedBankTransaction,
            auto_pair: bool,
            request: Request | None = None,
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
            )
            if existing is not None:
                return existing, False, bool(existing.matched_invoice_id)

            row = BankTransaction(
                subject_bank_account_id=int(account.id),
                provider=imported.provider,
                external_id=imported.external_id,
                booked_on=imported.booked_on,
                amount_cents=int(imported.amount_cents),
                currency=str(imported.currency or "CZK").upper()[:3] or "CZK",
                direction=imported.direction,
                variable_symbol=(
                    imported.variable_symbol
                    or (_bank_transaction_variable_symbol_candidates(imported.message)[0] if _bank_transaction_variable_symbol_candidates(imported.message) else None)
                ),
                constant_symbol=imported.constant_symbol,
                specific_symbol=imported.specific_symbol,
                counterparty_account=_limit_optional(imported.counterparty_account, 255),
                counterparty_name=_limit_optional(imported.counterparty_name, 255),
                message=(str(imported.message or "").strip() or None),
                raw_payload_json=_json_dumps_safe(imported.raw_payload),
            )
            db.add(row)
            db.flush()

            matched = False
            if auto_pair and _match_bank_transaction_row(
                db,
                account=account,
                row=row,
                request=request,
            ):
                matched = True
            return row, True, matched

        def _parse_email_bank_transaction(
            imported: ImportedBankEmail,
            *,
            parser_name: str | None,
        ) -> ImportedBankTransaction | None:
            parser = _normalize_payment_sync_email_parser(parser_name)
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
            raise BankSyncError("Zvolený parser bankovních e-mailů zatím neumíme zpracovat.")

        def _rehydrate_imported_email(row: BankIncomingEmail) -> ImportedBankEmail:
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
            db: Session,
            *,
            account: SubjectBankAccount,
            email_row: BankIncomingEmail,
            auto_pair: bool,
            parser_name: str | None,
            request: Request | None = None,
        ) -> tuple[str, bool]:
            imported = _rehydrate_imported_email(email_row)
            parsed = _parse_email_bank_transaction(imported, parser_name=parser_name)
            if parsed is None:
                email_row.processing_status = "stored"
                email_row.processing_note = "Uložené bez parseru bankovních e-mailů."
                db.add(email_row)
                return "stored", False

            transaction_row, was_imported, matched = _import_bank_transaction_row(
                db,
                account=account,
                imported=parsed,
                auto_pair=auto_pair,
                request=request,
            )
            email_row.matched_bank_transaction_id = int(transaction_row.id)
            if matched:
                email_row.processing_status = "matched"
                email_row.processing_note = "E-mail byl rozpoznán a platba spárována s fakturou."
            else:
                email_row.processing_status = "parsed_unmatched"
                email_row.processing_note = (
                    "E-mail byl rozpoznán, ale platbu se nepodařilo jednoznačně spárovat s fakturou."
                )
            db.add(email_row)
            return ("imported" if was_imported else "skipped_existing"), matched

        def _retry_existing_bank_emails(
            db: Session,
            *,
            account: SubjectBankAccount,
            auto_pair: bool,
            parser_name: str | None,
            request: Request | None = None,
        ) -> dict[str, int]:
            stats = {"imported": 0, "matched": 0, "unmatched": 0, "skipped_existing": 0}
            parser = _normalize_payment_sync_email_parser(parser_name)
            if parser == "pending":
                return stats

            rows = db.scalars(
                select(BankIncomingEmail)
                .where(BankIncomingEmail.subject_bank_account_id == int(account.id))
                .where(BankIncomingEmail.provider == "email_bank")
                .where(BankIncomingEmail.processing_status.in_(["stored", "parse_failed"]))
                .order_by(BankIncomingEmail.received_at.asc(), BankIncomingEmail.id.asc())
            ).all()
            for row in rows:
                try:
                    outcome, matched = _process_bank_incoming_email_row(
                        db,
                        account=account,
                        email_row=row,
                        auto_pair=auto_pair,
                        parser_name=parser,
                        request=request,
                    )
                except BankSyncError as exc:
                    row.processing_status = "parse_failed"
                    row.processing_note = str(exc)
                    db.add(row)
                    continue
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

        def _sync_subject_bank_account_email(
            db: Session,
            *,
            account: SubjectBankAccount,
            result: dict[str, object],
            request: Request | None = None,
        ) -> dict[str, object]:
            imap_host = str(getattr(settings, "payment_sync_imap_host", "") or "").strip()
            imap_username = str(getattr(settings, "payment_sync_imap_username", "") or "").strip()
            imap_password = str(getattr(settings, "payment_sync_imap_password", "") or "")
            parser_name = _normalize_payment_sync_email_parser(getattr(account, "payment_sync_email_parser", None))
            auto_pair = bool(getattr(account, "payment_sync_auto_pair", True))
            if not imap_host or not imap_username or not imap_password:
                account.payment_sync_last_error = "IMAP schránka pro bankovní notifikace zatím není nastavená."
                db.add(account)
                return result

            sender_filter = str(getattr(account, "payment_sync_email_sender_filter", "") or "").strip().lower()
            subject_filter = str(getattr(account, "payment_sync_email_subject_filter", "") or "").strip().lower()
            recipient_filter = _payment_sync_alert_email_for_account(account).strip().lower()

            previous_last_email_uid = str(getattr(account, "payment_sync_last_email_uid", "") or "").strip()

            try:
                imported_emails = fetch_imap_bank_emails(
                    host=imap_host,
                    port=int(getattr(settings, "payment_sync_imap_port", 993) or 993),
                    username=imap_username,
                    password=imap_password,
                    mailbox=str(getattr(settings, "payment_sync_imap_mailbox", "INBOX") or "INBOX"),
                    use_ssl=bool(getattr(settings, "payment_sync_imap_use_ssl", True)),
                    since_uid=str(getattr(account, "payment_sync_last_email_uid", "") or "").strip() or None,
                )
            except BankSyncError as exc:
                logging.getLogger("fakturek").exception(
                    "IMAP bank sync failed for bank account %s",
                    getattr(account, "id", "?"),
                )
                account.payment_sync_last_error = safe_bank_sync_error_message(exc)
                result["errors"].append(account.payment_sync_last_error)
                db.add(account)
                return result

            result["fetched"] = len(imported_emails)
            highest_uid = previous_last_email_uid

            if not previous_last_email_uid and imported_emails:
                for imported in imported_emails:
                    if str(imported.imap_uid or "").strip():
                        if not highest_uid or (highest_uid.isdigit() and imported.imap_uid.isdigit() and int(imported.imap_uid) > int(highest_uid)):
                            highest_uid = imported.imap_uid
                if highest_uid:
                    account.payment_sync_last_email_uid = highest_uid
                account.payment_sync_last_success_at = utc_now()
                account.payment_sync_last_error = None
                db.add(account)
                result["baseline_seeded"] = True
                return result

            for imported in imported_emails:
                if str(imported.imap_uid or "").strip():
                    if not highest_uid or (highest_uid.isdigit() and imported.imap_uid.isdigit() and int(imported.imap_uid) > int(highest_uid)):
                        highest_uid = imported.imap_uid

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
                )
                if existing is not None:
                    result["skipped_existing"] = int(result["skipped_existing"]) + 1
                    continue

                email_row = BankIncomingEmail(
                    subject_bank_account_id=int(account.id),
                    provider=imported.provider,
                    imap_uid=imported.imap_uid,
                    external_message_id=imported.external_message_id,
                    received_at=imported.received_at,
                    from_email=imported.from_email,
                    subject=imported.subject,
                    body_text=imported.body_text,
                    raw_headers_json=_json_dumps_safe(imported.raw_headers),
                    processing_status="stored",
                    processing_note="E-mail uložený pro párování bankovní platby.",
                )
                db.add(email_row)
                db.flush()

                try:
                    outcome, matched = _process_bank_incoming_email_row(
                        db,
                        account=account,
                        email_row=email_row,
                        auto_pair=auto_pair,
                        parser_name=parser_name,
                        request=request,
                    )
                except BankSyncError as exc:
                    email_row.processing_status = "parse_failed"
                    email_row.processing_note = str(exc)
                    db.add(email_row)
                    result["unmatched"] = int(result["unmatched"]) + 1
                    continue

                if outcome == "imported":
                    result["imported"] = int(result["imported"]) + 1
                elif outcome == "skipped_existing":
                    result["skipped_existing"] = int(result["skipped_existing"]) + 1
                else:
                    result["unmatched"] = int(result["unmatched"]) + 1
                if matched:
                    result["matched"] = int(result["matched"]) + 1
                elif outcome in {"imported", "stored"}:
                    result["unmatched"] = int(result["unmatched"]) + 1

            retry_stats = _retry_existing_bank_emails(
                db,
                account=account,
                auto_pair=auto_pair,
                parser_name=parser_name,
                request=request,
            )
            for key, value in retry_stats.items():
                result[key] = int(result.get(key) or 0) + int(value)
            if auto_pair:
                result["matched"] = int(result["matched"]) + _retry_existing_unmatched_bank_transactions(
                    db,
                    account=account,
                    request=request,
                )

            if highest_uid:
                account.payment_sync_last_email_uid = highest_uid
            account.payment_sync_last_success_at = utc_now()
            account.payment_sync_last_error = None
            db.add(account)
            return result

        def _sync_subject_bank_account(
            db: Session,
            *,
            account: SubjectBankAccount,
            request: Request | None = None,
        ) -> dict[str, object]:
            provider = _normalize_payment_sync_provider(getattr(account, "payment_sync_provider", None))
            enabled = bool(getattr(account, "payment_sync_enabled", False))
            auto_pair = bool(getattr(account, "payment_sync_auto_pair", True))
            now_utc = utc_now()
            result = {
                "account_id": int(account.id),
                "provider": provider,
                "fetched": 0,
                "imported": 0,
                "matched": 0,
                "unmatched": 0,
                "skipped_existing": 0,
                "errors": [],
            }

            account.payment_sync_last_checked_at = now_utc
            db.add(account)

            if not enabled or provider == "none":
                account.payment_sync_last_error = None
                return result

            if provider == "email_bank":
                return _sync_subject_bank_account_email(db, account=account, result=result, request=request)

            if provider != "fio_api":
                account.payment_sync_last_error = "Tento provider zatím neumíme synchronizovat."
                result["errors"].append(str(account.payment_sync_last_error))
                return result

            token = _decode_fio_api_token(getattr(account, "fio_api_token", None))
            if not token:
                account.payment_sync_last_error = "Chybí Fio API token."
                result["errors"].append(str(account.payment_sync_last_error))
                return result

            date_from, date_to = _payment_sync_date_window(account)

            try:
                fio_timeout_seconds = float(getattr(settings, "fio_timeout_seconds", 30.0) or 30.0)
                imported_transactions = []
                for attempt in range(1, 3):
                    try:
                        imported_transactions = fetch_fio_transactions(
                            token,
                            date_from=date_from,
                            date_to=date_to,
                            base_url=str(getattr(settings, "fio_api_base_url", "") or ""),
                            timeout_seconds=fio_timeout_seconds,
                        )
                        break
                    except BankSyncError:
                        if attempt >= 2:
                            raise
                        logging.getLogger("fakturek").warning(
                            "Fio API fetch failed for bank account %s; retrying once",
                            getattr(account, "id", "?"),
                        )
                        time.sleep(5)
            except BankSyncError as exc:
                logging.getLogger("fakturek").exception(
                    "Fio bank sync failed for bank account %s",
                    getattr(account, "id", "?"),
                )
                account.payment_sync_last_error = safe_bank_sync_error_message(exc)
                result["errors"].append(account.payment_sync_last_error)
                if auto_pair:
                    result["matched"] = int(result["matched"]) + _retry_existing_unmatched_bank_transactions(
                        db,
                        account=account,
                        request=request,
                    )
                return result

            result["fetched"] = len(imported_transactions)
            newest_booked_on = date_from

            for imported in imported_transactions:
                if imported.booked_on > newest_booked_on:
                    newest_booked_on = imported.booked_on
                if imported.direction != "incoming" or int(imported.amount_cents) <= 0:
                    continue

                row, was_imported, matched = _import_bank_transaction_row(
                    db,
                    account=account,
                    imported=imported,
                    auto_pair=auto_pair,
                    request=request,
                )
                if not was_imported:
                    result["skipped_existing"] = int(result["skipped_existing"]) + 1
                    continue
                result["imported"] = int(result["imported"]) + 1
                if matched:
                    result["matched"] = int(result["matched"]) + 1
                else:
                    result["unmatched"] = int(result["unmatched"]) + 1

            if auto_pair:
                result["matched"] = int(result["matched"]) + _retry_existing_unmatched_bank_transactions(
                    db,
                    account=account,
                    request=request,
                )

            account.payment_sync_cursor_date = newest_booked_on if imported_transactions else date_to
            account.payment_sync_last_success_at = now_utc
            account.payment_sync_last_error = None
            db.add(account)
            return result

        def _process_bank_sync(
            db: Session,
            *,
            request: Request | None = None,
            subject_id: int | None = None,
            account_id: int | None = None,
        ) -> dict[str, object]:
            stmt = select(SubjectBankAccount).order_by(SubjectBankAccount.subject_id.asc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
            if subject_id is not None:
                stmt = stmt.where(SubjectBankAccount.subject_id == int(subject_id))
            if account_id is not None:
                stmt = stmt.where(SubjectBankAccount.id == int(account_id))
            accounts = db.scalars(stmt).all()

            summary = {
                "accounts": [],
                "errors": [],
                "fetched": 0,
                "imported": 0,
                "matched": 0,
                "unmatched": 0,
                "skipped_existing": 0,
                "baseline_seeded": False,
            }
            for account in accounts:
                account_result = _sync_subject_bank_account(db, account=account, request=request)
                summary["accounts"].append(account_result)
                for key in ("fetched", "imported", "matched", "unmatched", "skipped_existing"):
                    summary[key] = int(summary[key]) + int(account_result.get(key) or 0)
                if account_result.get("baseline_seeded"):
                    summary["baseline_seeded"] = True
                summary["errors"].extend(list(account_result.get("errors") or []))
            db.commit()
            return summary

        def _bank_sync_notice(result: dict[str, object]) -> str:
            if result.get("feature_disabled"):
                return "Bank sync je vypnutý správcem instalace."
            if result.get("baseline_seeded"):
                return (
                    "Párování je připravené od této chvíle. "
                    f"Založil se výchozí bod a další sync už bude brát jen nové pohyby. "
                    f"Načteno {int(result.get('fetched') or 0)} záznamů, starší historie se teď nepárovala."
                )
            return (
                f"Načteno {int(result.get('fetched') or 0)} pohybů, "
                f"uloženo {int(result.get('imported') or 0)}, "
                f"spárováno {int(result.get('matched') or 0)}, "
                f"bez shody {int(result.get('unmatched') or 0)}."
            )

        def _blank_subject_prefill(*, issuer: dict | None = None, subject: Subject | None = None) -> dict[str, object]:
            issuer_dict = issuer or {}
            return {
                "name": "",
                "email": "",
                "phone": "",
                "street": "",
                "city": "",
                "zip": "",
                "country": (getattr(subject, "country", None) or issuer_dict.get("country") or "CZ"),
                "ico": "",
                "dic": "",
                "is_vat_payer": False,
                "is_vat_identified_person": False,
                "tax_regime": "standard",
                "flat_tax_band": "1",
                "flat_tax_income_profile": "general",
                "default_currency": (getattr(subject, "default_currency", None) or issuer_dict.get("default_currency") or "CZK"),
                "switch_after_create": True,
            }

        def _blank_user_access_prefill() -> dict[str, object]:
            return {
                "username": "",
                "email": "",
                "password": "",
                "role": "manager",
                "can_view": True,
                "can_edit": True,
                "can_issue": True,
                "can_export": True,
            }

        def _blank_isolated_account_prefill(
            *,
            issuer: dict | None = None,
            subject: Subject | None = None,
        ) -> dict[str, object]:
            issuer_dict = issuer or {}
            default_email = str(getattr(subject, "email", None) or issuer_dict.get("email") or "")
            default_country = str(getattr(subject, "country", None) or issuer_dict.get("country") or "CZ")
            default_currency = str(
                getattr(subject, "default_currency", None) or issuer_dict.get("default_currency") or "CZK"
            )
            return {
                "username": "",
                "email": "",
                "password": "",
                "subject_name": "",
                "subject_email": default_email,
                "subject_phone": "",
                "subject_street": "",
                "subject_city": "",
                "subject_zip": "",
                "subject_ico": "",
                "subject_dic": "",
                "subject_country": default_country,
                "subject_default_currency": default_currency,
            }

        def _lookup_subject_prefill_from_registry(
            db: Session,
            *,
            prefill: dict[str, object],
            prefix: str = "",
        ) -> tuple[dict[str, object], str | None, str | None]:
            ico_key = f"{prefix}ico"
            country_key = f"{prefix}country"
            name_key = f"{prefix}name"
            street_key = f"{prefix}street"
            city_key = f"{prefix}city"
            zip_key = f"{prefix}zip"
            dic_key = f"{prefix}dic"

            ico_raw = str(prefill.get(ico_key) or "").strip()
            country = str(prefill.get(country_key) or "CZ").strip().upper() or "CZ"
            if not ico_raw:
                return prefill, None, "Zadej IČO, které chceš načíst z registru."
            if country not in {"CZ", "SK"}:
                return prefill, None, "Automatické načtení z registru teď podporuje jen CZ a SK subjekty."

            from fakturek.company_lookup import (
                CompanyLookupError,
                lookup_cz_company_prefill_with_cache,
                lookup_sk_company_prefill_with_cache,
            )
            from fakturek.settings import get_settings

            app_settings = get_settings()
            try:
                if country == "SK":
                    company, source, provider = lookup_sk_company_prefill_with_cache(
                        db,
                        ico_raw,
                        rpo_base_url=app_settings.sk_rpo_base_url,
                        rpo_timeout_seconds=app_settings.sk_rpo_timeout_seconds,
                        orsr_base_url=app_settings.sk_orsr_base_url,
                        orsr_timeout_seconds=app_settings.sk_orsr_timeout_seconds,
                        cache_ttl_days=app_settings.company_lookup_cache_ttl_days,
                    )
                    label = "RPO" if provider == "rpo" else "ORSR"
                    info = (
                        f"Načteno ze slovenského registru {label}."
                        if source == "live"
                        else f"Načteno ze slovenského registru {label} (cache)."
                    )
                else:
                    company, source = lookup_cz_company_prefill_with_cache(
                        db,
                        ico_raw,
                        base_url=app_settings.ares_base_url,
                        timeout_seconds=app_settings.ares_timeout_seconds,
                        cache_ttl_days=app_settings.company_lookup_cache_ttl_days,
                    )
                    info = (
                        "Načteno z českého registru ARES."
                        if source == "live"
                        else "Načteno z českého registru ARES (cache)."
                    )
            except CompanyLookupError as exc:
                return prefill, None, str(exc)

            if company.name:
                prefill[name_key] = company.name
            prefill[street_key] = company.street
            prefill[city_key] = company.city
            prefill[zip_key] = company.zip
            prefill[country_key] = company.country or country
            prefill[ico_key] = company.ico
            prefill[dic_key] = company.dic
            return prefill, info, None

        def _blank_existing_user_link_prefill() -> dict[str, object]:
            return {
                "identifier": "",
                "role": "manager",
                "can_view": True,
                "can_edit": True,
                "can_issue": True,
                "can_export": True,
            }

        def _subject_access_prefill_from_link(link: UserSubject) -> dict[str, object]:
            return {
                "role": str(getattr(link, "role", "") or "user"),
                "can_view": bool(getattr(link, "can_view", False)),
                "can_edit": bool(getattr(link, "can_edit", False)),
                "can_issue": bool(getattr(link, "can_issue", False)),
                "can_export": bool(getattr(link, "can_export", False)),
            }

        def _accessible_subjects_view_rows(
            db: Session,
            *,
            request: Request,
            user_id: int | None,
            current_subject_id: int,
            topbar_next_target: str | None = None,
        ) -> list[dict[str, object]]:
            if user_id is None:
                return []
            try:
                links = db.scalars(
                    select(UserSubject)
                    .where(UserSubject.user_id == int(user_id))
                    .order_by(UserSubject.subject_id.asc(), UserSubject.id.asc())
                ).all()
            except SQLAlchemyError:
                return []
            rows: list[dict[str, object]] = []
            for link in links:
                if not bool(getattr(link, "can_view", False)):
                    continue
                subject = db.get(Subject, int(link.subject_id))
                if subject is None:
                    continue
                sid = int(subject.id)
                can_manage = _user_can_manage_subject(db, user_id=user_id, subject_id=sid)
                can_manage_users = _user_can_manage_subject_users(db, user_id=user_id, subject_id=sid)
                topbar_target = _safe_next_url(topbar_next_target, "/")
                rows.append(
                    {
                        "id": sid,
                        "name": str(getattr(subject, "name", "") or f"Subjekt {sid}"),
                        "ico": str(getattr(subject, "ico", "") or ""),
                        "country": str(getattr(subject, "country", "") or ""),
                        "default_currency": str(getattr(subject, "default_currency", "") or "CZK"),
                        "role": str(getattr(link, "role", "") or "user"),
                        "can_manage": can_manage,
                        "can_manage_users": can_manage_users,
                        "is_current": sid == int(current_subject_id),
                        "switch_url": _subject_switch_url(request, subject_id=sid, next_url="/settings#subjects-admin"),
                        "switch_form": _subject_switch_form_values(request, subject_id=sid, next_url="/settings#subjects-admin"),
                        "topbar_switch_url": _subject_switch_url(request, subject_id=sid, next_url=topbar_target),
                        "topbar_switch_form": _subject_switch_form_values(request, subject_id=sid, next_url=topbar_target),
                    }
                )
            return rows

        def _subject_access_users_view_rows(
            db: Session,
            *,
            subject_id: int,
            actor_user_id: int | None = None,
            access_edit_prefills: dict[int, dict[str, object]] | None = None,
        ) -> list[dict[str, object]]:
            prefill_map = {int(key): dict(value) for key, value in (access_edit_prefills or {}).items()}
            actor_can_manage_users = _user_can_manage_subject_users(
                db,
                user_id=actor_user_id,
                subject_id=subject_id,
            )
            actor_is_owner = _user_can_manage_subject(db, user_id=actor_user_id, subject_id=subject_id)
            owner_count = _subject_owner_count(db, subject_id=subject_id)
            try:
                links = db.scalars(
                    select(UserSubject)
                    .where(UserSubject.subject_id == int(subject_id))
                    .order_by(UserSubject.role.asc(), UserSubject.id.asc())
                ).all()
            except SQLAlchemyError:
                return []
            rows: list[dict[str, object]] = []
            for link in links:
                user = db.get(User, int(link.user_id))
                if user is None:
                    continue
                current_role = str(getattr(link, "role", "") or "user")
                can_manage_link = _user_can_manage_subject_link(
                    db,
                    user_id=actor_user_id,
                    subject_id=subject_id,
                    link=link,
                )
                role_options = _subject_user_role_options(db, user_id=actor_user_id, subject_id=subject_id)
                if current_role == "owner" and owner_count <= 1:
                    role_options = ["owner"]
                manage_hint = ""
                if not actor_can_manage_users:
                    manage_hint = "Přístupy u tohohle subjektu teď spravovat nemůžeš."
                elif not can_manage_link and current_role == "owner":
                    manage_hint = "Owner přístupy může upravovat nebo mazat jen owner subjektu."
                elif current_role == "owner" and owner_count <= 1:
                    manage_hint = "Posledního ownera nejde odebrat ani degradovat."
                row_form = prefill_map.get(int(link.id), _subject_access_prefill_from_link(link))
                rows.append(
                    {
                        "id": int(link.id),
                        "user_id": int(user.id),
                        "username": str(getattr(user, "username", "") or ""),
                        "email": str(getattr(user, "email", "") or ""),
                        "role": current_role,
                        "can_view": bool(getattr(link, "can_view", False)),
                        "can_edit": bool(getattr(link, "can_edit", False)),
                        "can_issue": bool(getattr(link, "can_issue", False)),
                        "can_export": bool(getattr(link, "can_export", False)),
                        "is_self": actor_user_id is not None and int(user.id) == int(actor_user_id),
                        "can_manage_link": can_manage_link,
                        "can_delete_link": bool(can_manage_link and not (current_role == "owner" and owner_count <= 1)),
                        "manage_hint": manage_hint,
                        "role_options": list(role_options or [current_role]),
                        "form": row_form,
                        "actor_is_owner": actor_is_owner,
                    }
                )
            return rows




































        # ------------------------------------------------------------------
        # ADMIN-08 – system health + background jobs panel
        # ------------------------------------------------------------------




















        # ------------------------------------------------------------------
        # ADMIN-09 – error log viewer + monitoring
        # ------------------------------------------------------------------












        # ------------------------------------------------------------------
        # ADMIN-10: global search, GDPR and exports
        # ------------------------------------------------------------------















































































































        def _settings_subject_admin_context(
            db: Session,
            *,
            request: Request,
            issuer: dict,
            subject: Subject | None = None,
            subject_prefill: dict | None = None,
            user_access_prefill: dict | None = None,
            existing_user_link_prefill: dict | None = None,
            access_edit_prefills: dict[int, dict[str, object]] | None = None,
        ) -> dict[str, object]:
            current_sid = _current_subject_id()
            current_subject_row = subject or _load_subject_for_current_session(db)
            user_id = _current_user_id_or_none(request)
            current_subject_payload = {
                "id": int(getattr(current_subject_row, "id", current_sid) or current_sid),
                "name": str(getattr(current_subject_row, "name", "") or f"Subjekt {current_sid}"),
                "ico": str(getattr(current_subject_row, "ico", "") or ""),
                "country": str(getattr(current_subject_row, "country", "") or (issuer.get("country") if isinstance(issuer, dict) else "CZ") or "CZ"),
                "default_currency": str(getattr(current_subject_row, "default_currency", "") or issuer.get("default_currency") or "CZK"),
                "public_username": str(getattr(current_subject_row, "public_username", "") or ""),
            }
            can_manage_subject = _user_can_manage_subject(db, user_id=user_id, subject_id=current_sid)
            can_manage_subject_users = _user_can_manage_subject_users(db, user_id=user_id, subject_id=current_sid)
            current_link = None
            if user_id is not None and current_sid is not None:
                try:
                    current_link = db.scalar(
                        select(UserSubject).where(
                            UserSubject.user_id == int(user_id),
                            UserSubject.subject_id == int(current_sid),
                        )
                    )
                except SQLAlchemyError:
                    current_link = None
            can_edit_subject = bool(getattr(current_link, "can_edit", False))
            return {
                "current_subject": current_subject_payload,
                "accessible_subjects": _accessible_subjects_view_rows(
                    db,
                    request=request,
                    user_id=user_id,
                    current_subject_id=current_sid,
                ),
                "can_edit_subject": can_edit_subject,
                "can_manage_subject": can_manage_subject,
                "can_manage_subject_users": can_manage_subject_users,
                "subject_users": _subject_access_users_view_rows(db, subject_id=current_sid, actor_user_id=user_id, access_edit_prefills=access_edit_prefills) if can_manage_subject_users else [],
                "subject_user_role_options": _subject_user_role_options(db, user_id=user_id, subject_id=current_sid),
                "subject_prefill": subject_prefill or _blank_subject_prefill(issuer=issuer, subject=current_subject_row),
                "user_access_prefill": user_access_prefill or _blank_user_access_prefill(),
                "existing_user_link_prefill": existing_user_link_prefill or _blank_existing_user_link_prefill(),
            }

        def _subject_nav_template_context(request: Request) -> dict[str, object]:
            empty_payload = {
                "current_subject_nav": {},
                "accessible_subjects_nav": [],
                "ui_theme_options": UI_THEME_OPTIONS,
            }
            if not _db_enabled:
                return empty_payload

            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return empty_payload

            try:
                from fakturek.db import get_sessionmaker  # type: ignore

                SessionLocal = get_sessionmaker()
                with SessionLocal() as db:  # type: ignore
                    _refresh_current_session_access_context(request, db)
                    try:
                        raw_sid = request.session.get("subject_id")
                        current_sid = int(raw_sid) if raw_sid is not None else DEFAULT_SUBJECT_ID
                    except Exception:
                        current_sid = DEFAULT_SUBJECT_ID

                    subject = db.get(Subject, int(current_sid))
                    return {
                        "ui_theme_options": UI_THEME_OPTIONS,
                        "ui_language_options": UI_LANGUAGE_OPTIONS,
                        "current_ui_language": _normalize_ui_language(request.session.get("ui_language") if hasattr(request, "session") else "cs"),
                        "current_subject_nav": {
                            "id": int(getattr(subject, "id", current_sid) or current_sid),
                            "name": str(getattr(subject, "name", "") or f"Subjekt {current_sid}"),
                            "ico": str(getattr(subject, "ico", "") or ""),
                            "country": str(getattr(subject, "country", "") or "CZ"),
                            "default_currency": str(getattr(subject, "default_currency", "") or "CZK"),
                        },
                        "accessible_subjects_nav": _accessible_subjects_view_rows(
                            db,
                            request=request,
                            user_id=user_id,
                            current_subject_id=current_sid,
                            topbar_next_target=_topbar_subject_switch_target(request),
                        ),
                    }
            except Exception:
                logging.getLogger("fakturek").exception("Failed to build subject switcher context")
                return empty_payload

        templates.context_processors.append(_subject_nav_template_context)

        def _subject_flags(
            db: Session, subject_override: Subject | None = None
        ) -> tuple[bool, str]:
            """Return (is_vat_payer, default_currency) for current subject."""

            subject = subject_override or _load_subject_for_current_session(db)
            is_vat_payer = bool(getattr(subject, "is_vat_payer", False)) if subject else False
            cur = (getattr(subject, "default_currency", None) or "CZK").strip().upper()
            if len(cur) != 3:
                cur = "CZK"
            return is_vat_payer, cur

        def _export_subject_slug(subject: Subject | None, *, subject_id: int) -> str:
            base = (getattr(subject, "name", "") or "").strip()
            if not base:
                base = f"subject-{int(subject_id)}"
            return safe_filename_base(base, fallback=f"subject-{int(subject_id)}")

        def _load_export_contacts(
            db: Session,
            *,
            subject_id: int,
            q: str | None = None,
            contact_ids: list[int] | None = None,
        ) -> list[Contact]:
            stmt = select(Contact).where(Contact.subject_id == int(subject_id))
            if contact_ids:
                stmt = stmt.where(Contact.id.in_([int(item_id) for item_id in contact_ids if int(item_id) > 0]))
            q_clean = " ".join(str(q or "").split()).strip()
            if q_clean:
                like = f"%{q_clean}%"
                stmt = stmt.where(
                    or_(
                        Contact.name.like(like),
                        Contact.email.like(like),
                        Contact.ico.like(like),
                    )
                )
            stmt = stmt.order_by(Contact.name.asc(), Contact.id.asc())
            return db.scalars(stmt).all()

        def _export_contacts_rows(
            db: Session,
            *,
            subject_id: int,
            q: str | None = None,
            contact_ids: list[int] | None = None,
        ) -> list[dict[str, object]]:
            rows = _load_export_contacts(
                db,
                subject_id=int(subject_id),
                q=q,
                contact_ids=contact_ids,
            )
            payload: list[dict[str, object]] = []
            for contact in rows:
                payload.append(
                    {
                        "id": int(contact.id),
                        "name": str(contact.name or ""),
                        "email": str(contact.email or ""),
                        "phone": str(contact.phone or ""),
                        "street": str(contact.street or ""),
                        "city": str(contact.city or ""),
                        "zip": str(contact.zip or ""),
                        "country": str(contact.country or ""),
                        "ico": str(contact.ico or ""),
                        "dic": str(contact.dic or ""),
                        "external_source": str(contact.external_source or ""),
                        "external_id": str(contact.external_id or ""),
                        "created_at": _iso_to_export_str(getattr(contact, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(contact, "updated_at", None)),
                    }
                )
            return payload

        def _invoice_newest_first_ordering():
            return (
                Invoice.issue_date.desc(),
                Invoice.number.desc(),
                Invoice.created_at.desc(),
                Invoice.id.desc(),
            )

        def _build_invoice_export_stmt(
            *,
            subject_id: int,
            q: str | None = None,
            status: str | None = None,
            contact_id: int | None = None,
            contact_ids: list[int] | None = None,
            document_type: str | None = None,
            overdue: bool = False,
            issue_date_from: date | None = None,
            issue_date_to: date | None = None,
            today_value: date | None = None,
        ):
            today_local = today_value or date.today()
            stmt = (
                select(Invoice)
                .where(Invoice.subject_id == int(subject_id))
                .where(Invoice.contact.has(Contact.subject_id == int(subject_id)))
                .where(_invoice_visible_in_lists_clause())
                .options(selectinload(Invoice.contact), selectinload(Invoice.series))
            )

            q_clean = (q or "").strip()
            status_clean = (status or "").strip()
            document_type_clean = _invoice_document_type_filter_value(document_type)
            if status_clean and status_clean in ALLOWED_INVOICE_STATUSES:
                stmt = stmt.where(Invoice.status == status_clean)

            if document_type_clean:
                stmt = stmt.where(Invoice.document_type == document_type_clean)

            normalized_contact_ids = [int(item_id) for item_id in list(contact_ids or []) if int(item_id) > 0]
            if normalized_contact_ids:
                stmt = stmt.where(Invoice.contact_id.in_(normalized_contact_ids))
            elif contact_id:
                stmt = stmt.where(Invoice.contact_id == int(contact_id))

            if q_clean:
                like = f"%{q_clean}%"
                stmt = stmt.where(
                    or_(
                        Invoice.number.like(like),
                        Invoice.contact.has(Contact.name.like(like)),
                    )
                )

            if overdue:
                stmt = stmt.where(Invoice.due_date < today_local).where(Invoice.status != "paid")

            if issue_date_from is not None:
                stmt = stmt.where(Invoice.issue_date >= issue_date_from)

            if issue_date_to is not None:
                stmt = stmt.where(Invoice.issue_date <= issue_date_to)

            return stmt.order_by(*_invoice_newest_first_ordering())

        def _load_export_invoices(
            db: Session,
            *,
            subject_id: int,
            q: str | None = None,
            status: str | None = None,
            contact_id: int | None = None,
            contact_ids: list[int] | None = None,
            document_type: str | None = None,
            overdue: bool = False,
            issue_date_from: date | None = None,
            issue_date_to: date | None = None,
        ) -> list[Invoice]:
            stmt = _build_invoice_export_stmt(
                subject_id=int(subject_id),
                q=q,
                status=status,
                contact_id=contact_id,
                contact_ids=contact_ids,
                document_type=document_type,
                overdue=bool(overdue),
                issue_date_from=issue_date_from,
                issue_date_to=issue_date_to,
            )
            return db.scalars(stmt).all()

        def _export_invoices_rows(
            db: Session,
            *,
            subject_id: int,
            q: str | None = None,
            status: str | None = None,
            contact_id: int | None = None,
            contact_ids: list[int] | None = None,
            document_type: str | None = None,
            overdue: bool = False,
            issue_date_from: date | None = None,
            issue_date_to: date | None = None,
        ) -> list[dict[str, object]]:
            invoices = _load_export_invoices(
                db,
                subject_id=int(subject_id),
                q=q,
                status=status,
                contact_id=contact_id,
                contact_ids=contact_ids,
                document_type=document_type,
                overdue=bool(overdue),
                issue_date_from=issue_date_from,
                issue_date_to=issue_date_to,
            )
            payload: list[dict[str, object]] = []
            for invoice in invoices:
                contact = getattr(invoice, "contact", None)
                series = getattr(invoice, "series", None)
                total_cents = int(getattr(invoice, "total_cents", 0) or 0)
                discount_cents = int(getattr(invoice, "discount_cents", 0) or 0)
                rounding_adjustment_cents = int(getattr(invoice, "rounding_adjustment_cents", 0) or 0)
                subtotal_after_discount_cents = int(total_cents - rounding_adjustment_cents)
                items_total_cents = int(subtotal_after_discount_cents + discount_cents)
                payload.append(
                    {
                        "id": int(invoice.id),
                        "number": str(invoice.number or ""),
                        "document_type": _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice")),
                        "source_invoice_id": int(getattr(invoice, "source_invoice_id", 0) or 0),
                        "source_invoice_number": _invoice_source_invoice_number(db, invoice=invoice),
                        "status": str(invoice.status or ""),
                        "issue_date": _iso_to_export_str(getattr(invoice, "issue_date", None)),
                        "taxable_supply_date": _iso_to_export_str(_invoice_taxable_supply_date(invoice)),
                        "due_date": _iso_to_export_str(getattr(invoice, "due_date", None)),
                        "paid_on": _iso_to_export_str(getattr(invoice, "paid_on", None)),
                        "currency": str(invoice.currency or ""),
                        "items_total": _money_cents_to_export_str(items_total_cents),
                        "items_total_cents": int(items_total_cents),
                        "discount": _money_cents_to_export_str(discount_cents),
                        "discount_cents": int(discount_cents),
                        "subtotal_after_discount": _money_cents_to_export_str(subtotal_after_discount_cents),
                        "subtotal_after_discount_cents": int(subtotal_after_discount_cents),
                        "total": _money_cents_to_export_str(total_cents),
                        "total_cents": int(total_cents),
                        "rounding_adjustment": _money_cents_to_export_str(rounding_adjustment_cents),
                        "rounding_adjustment_cents": int(rounding_adjustment_cents),
                        "contact_id": int(getattr(invoice, "contact_id", 0) or 0),
                        "contact_name": str(getattr(contact, "name", "") or ""),
                        "contact_email": str(getattr(contact, "email", "") or ""),
                        "contact_ico": str(getattr(contact, "ico", "") or ""),
                        "series_name": str(getattr(series, "name", "") or ""),
                        "bank_account_label": str(getattr(invoice, "bank_account_label", "") or ""),
                        "bank_account_number": str(getattr(invoice, "bank_account_number", "") or ""),
                        "bank_account_iban": str(getattr(invoice, "bank_account_iban", "") or ""),
                        "bank_account_bic": str(getattr(invoice, "bank_account_bic", "") or ""),
                        "bank_account_country": str(getattr(invoice, "bank_account_country", "") or ""),
                        "notes": str(getattr(invoice, "notes", "") or ""),
                        "internal_notes": str(getattr(invoice, "internal_notes", "") or ""),
                        "issued_at": _iso_to_export_str(getattr(invoice, "issued_at", None)),
                        "sent_at": _iso_to_export_str(getattr(invoice, "sent_at", None)),
                        "reminder_sent_at": _iso_to_export_str(getattr(invoice, "reminder_sent_at", None)),
                        "public_url_enabled": "1" if (getattr(invoice, "public_token", None) or "").strip() else "0",
                        "pdf_generated_at": _iso_to_export_str(getattr(invoice, "pdf_generated_at", None)),
                        "created_at": _iso_to_export_str(getattr(invoice, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(invoice, "updated_at", None)),
                    }
                )
            return payload

        def _export_invoice_items_rows(
            db: Session,
            *,
            subject_id: int,
            invoice_ids: list[int] | None = None,
        ) -> list[dict[str, object]]:
            stmt = (
                select(InvoiceItem, Invoice)
                .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
                .where(Invoice.subject_id == int(subject_id))
            )
            normalized_invoice_ids = [int(invoice_id) for invoice_id in list(invoice_ids or []) if int(invoice_id) > 0]
            if normalized_invoice_ids:
                stmt = stmt.where(Invoice.id.in_(normalized_invoice_ids))
            rows = db.execute(
                stmt.order_by(*_invoice_newest_first_ordering(), InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
            ).all()
            payload: list[dict[str, object]] = []
            for item, invoice in rows:
                payload.append(
                    {
                        "invoice_id": int(invoice.id),
                        "invoice_number": str(invoice.number or ""),
                        "invoice_status": str(invoice.status or ""),
                        "issue_date": _iso_to_export_str(getattr(invoice, "issue_date", None)),
                        "taxable_supply_date": _iso_to_export_str(_invoice_taxable_supply_date(invoice)),
                        "currency": str(invoice.currency or ""),
                        "line_no": int(getattr(item, "sort_order", 0) or 0),
                        "description": str(getattr(item, "description", "") or ""),
                        "quantity": _decimal_to_export_str(getattr(item, "quantity", None)),
                        "unit": _normalize_invoice_item_unit(getattr(item, "unit", "")),
                        "unit_price": _money_cents_to_export_str(getattr(item, "unit_price_cents", 0)),
                        "unit_price_cents": int(getattr(item, "unit_price_cents", 0) or 0),
                        "vat_rate": _decimal_to_export_str(getattr(item, "vat_rate", None)),
                        "line_net": _money_cents_to_export_str(getattr(item, "line_net_cents", 0)),
                        "line_net_cents": int(getattr(item, "line_net_cents", 0) or 0),
                        "line_vat": _money_cents_to_export_str(getattr(item, "line_vat_cents", 0)),
                        "line_vat_cents": int(getattr(item, "line_vat_cents", 0) or 0),
                        "line_total": _money_cents_to_export_str(getattr(item, "line_total_cents", 0)),
                        "line_total_cents": int(getattr(item, "line_total_cents", 0) or 0),
                        "created_at": _iso_to_export_str(getattr(item, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(item, "updated_at", None)),
                    }
                )
            return payload

        def _export_bank_accounts_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            rows = db.scalars(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.subject_id == int(subject_id))
                .order_by(SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
            ).all()
            payload: list[dict[str, object]] = []
            for account in rows:
                payload.append(
                    {
                        "id": int(account.id),
                        "label": str(account.label or ""),
                        "account_number": str(account.account_number or ""),
                        "iban": str(account.iban or ""),
                        "bic": str(account.bic or ""),
                        "country": str(account.country or ""),
                        "is_default": "1" if bool(getattr(account, "is_default", False)) else "0",
                        "sort_order": int(getattr(account, "sort_order", 0) or 0),
                        "created_at": _iso_to_export_str(getattr(account, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(account, "updated_at", None)),
                    }
                )
            return payload

        def _export_subject_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            subject = db.get(Subject, int(subject_id))
            if subject is None:
                return []
            return [
                {
                    "id": int(subject.id),
                    "public_username": str(subject.public_username or ""),
                    "name": str(subject.name or ""),
                    "email": str(subject.email or ""),
                    "phone": str(subject.phone or ""),
                    "street": str(subject.street or ""),
                    "city": str(subject.city or ""),
                    "zip": str(subject.zip or ""),
                    "country": str(subject.country or ""),
                    "ico": str(subject.ico or ""),
                    "dic": str(subject.dic or ""),
                    "bank_account": str(subject.bank_account or ""),
                    "is_vat_payer": "1" if bool(getattr(subject, "is_vat_payer", False)) else "0",
                    "default_currency": str(subject.default_currency or "CZK"),
                    "created_at": _iso_to_export_str(getattr(subject, "created_at", None)),
                    "updated_at": _iso_to_export_str(getattr(subject, "updated_at", None)),
                }
            ]

        def _export_payments_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            rows = db.execute(
                select(Payment, Invoice)
                .join(Invoice, Invoice.id == Payment.invoice_id)
                .where(Invoice.subject_id == int(subject_id))
                .order_by(Payment.paid_on.desc(), Payment.id.desc())
            ).all()
            payload: list[dict[str, object]] = []
            for payment, invoice in rows:
                payload.append(
                    {
                        "id": int(payment.id),
                        "invoice_id": int(invoice.id),
                        "invoice_number": str(invoice.number or ""),
                        "paid_on": _iso_to_export_str(getattr(payment, "paid_on", None)),
                        "amount": _money_cents_to_export_str(getattr(payment, "amount_cents", 0)),
                        "amount_cents": int(getattr(payment, "amount_cents", 0) or 0),
                        "note": str(getattr(payment, "note", "") or ""),
                        "created_at": _iso_to_export_str(getattr(payment, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(payment, "updated_at", None)),
                    }
                )
            return payload

        def _export_invoice_emails_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            rows = db.execute(
                select(InvoiceEmail, Invoice)
                .join(Invoice, Invoice.id == InvoiceEmail.invoice_id)
                .where(Invoice.subject_id == int(subject_id))
                .order_by(InvoiceEmail.id.desc())
            ).all()
            payload: list[dict[str, object]] = []
            for email_row, invoice in rows:
                payload.append(
                    {
                        "id": int(email_row.id),
                        "invoice_id": int(invoice.id),
                        "invoice_number": str(invoice.number or ""),
                        "kind": str(getattr(email_row, "kind", "") or ""),
                        "from_email": str(getattr(email_row, "from_email", "") or ""),
                        "to_email": str(getattr(email_row, "to_email", "") or ""),
                        "subject": str(getattr(email_row, "subject", "") or ""),
                        "body": str(getattr(email_row, "body", "") or ""),
                        "status": str(getattr(email_row, "status", "") or ""),
                        "sent_at": _iso_to_export_str(getattr(email_row, "sent_at", None)),
                        "message_id": str(getattr(email_row, "message_id", "") or ""),
                        "error_message": (
                            "E-mail se nepodařilo odeslat."
                            if str(getattr(email_row, "error_message", "") or "").strip()
                            else ""
                        ),
                        "created_at": _iso_to_export_str(getattr(email_row, "created_at", None)),
                        "updated_at": _iso_to_export_str(getattr(email_row, "updated_at", None)),
                    }
                )
            return payload

        def _export_audit_rows(db: Session, *, subject_id: int) -> list[dict[str, object]]:
            rows = db.scalars(
                select(AuditLog)
                .where(AuditLog.subject_id == int(subject_id))
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            ).all()
            payload: list[dict[str, object]] = []
            for row in rows:
                payload.append(
                    {
                        "id": int(row.id),
                        "created_at": _iso_to_export_str(getattr(row, "created_at", None)),
                        "user_id": int(getattr(row, "user_id", 0) or 0),
                        "action": str(getattr(row, "action", "") or ""),
                        "entity_type": str(getattr(row, "entity_type", "") or ""),
                        "entity_id": int(getattr(row, "entity_id", 0) or 0),
                        "data_json": str(getattr(row, "data_json", "") or ""),
                        "ip": str(getattr(row, "ip", "") or ""),
                        "user_agent": str(getattr(row, "user_agent", "") or ""),
                    }
                )
            return payload

        def _build_full_export_zip_bytes(db: Session, *, subject_id: int) -> tuple[bytes, str]:
            subject = _load_subject_for_current_session(db)
            subject_slug = _export_subject_slug(subject, subject_id=int(subject_id))
            stamp = utc_now().strftime("%Y%m%d-%H%M%S")

            csv_datasets: list[tuple[str, list[str], list[dict[str, object]]]] = [
                (
                    "subject.csv",
                    [
                        "id",
                        "public_username",
                        "name",
                        "email",
                        "phone",
                        "street",
                        "city",
                        "zip",
                        "country",
                        "ico",
                        "dic",
                        "bank_account",
                        "is_vat_payer",
                        "default_currency",
                        "created_at",
                        "updated_at",
                    ],
                    _export_subject_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "contacts.csv",
                    [
                        "id",
                        "name",
                        "email",
                        "phone",
                        "street",
                        "city",
                        "zip",
                        "country",
                        "ico",
                        "dic",
                        "external_source",
                        "external_id",
                        "created_at",
                        "updated_at",
                    ],
                    _export_contacts_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "invoices.csv",
                    _invoice_export_fieldnames(),
                    _export_invoices_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "invoice_items.csv",
                    _invoice_item_export_fieldnames(),
                    _export_invoice_items_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "bank_accounts.csv",
                    [
                        "id",
                        "label",
                        "account_number",
                        "iban",
                        "bic",
                        "country",
                        "is_default",
                        "sort_order",
                        "created_at",
                        "updated_at",
                    ],
                    _export_bank_accounts_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "payments.csv",
                    [
                        "id",
                        "invoice_id",
                        "invoice_number",
                        "paid_on",
                        "amount",
                        "amount_cents",
                        "note",
                        "created_at",
                        "updated_at",
                    ],
                    _export_payments_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "invoice_emails.csv",
                    [
                        "id",
                        "invoice_id",
                        "invoice_number",
                        "kind",
                        "from_email",
                        "to_email",
                        "subject",
                        "body",
                        "status",
                        "sent_at",
                        "message_id",
                        "error_message",
                        "created_at",
                        "updated_at",
                    ],
                    _export_invoice_emails_rows(db, subject_id=int(subject_id)),
                ),
                (
                    "audit_log.csv",
                    [
                        "id",
                        "created_at",
                        "user_id",
                        "action",
                        "entity_type",
                        "entity_id",
                        "data_json",
                        "ip",
                        "user_agent",
                    ],
                    _export_audit_rows(db, subject_id=int(subject_id)),
                ),
            ]

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                summary_lines = [
                    "Fakturek – kompletní export dat",
                    f"subject_id={int(subject_id)}",
                    f"generated_at_utc={utc_now().isoformat(timespec='seconds')}",
                    "",
                    "Soubory:",
                ]
                for name, fieldnames, rows in csv_datasets:
                    zf.writestr(name, _csv_bytes_from_rows(fieldnames, rows))
                    summary_lines.append(f"- {name} ({len(rows)} řádků)")
                zf.writestr("README.txt", "\n".join(summary_lines).encode("utf-8"))
            return buf.getvalue(), f"{subject_slug}-export-{stamp}.zip"

        def _party_payload_from_subject(subject: Subject | None) -> dict:
            if subject is None:
                return {
                    "name": "",
                    "email": "",
                    "phone": "",
                    "street": "",
                    "city": "",
                    "zip": "",
                    "country": "CZ",
                    "ico": "",
                    "dic": "",
                }
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

        def _party_payload_from_contact(contact: Contact | None) -> dict:
            if contact is None:
                return {
                    "name": "",
                    "email": "",
                    "phone": "",
                    "street": "",
                    "city": "",
                    "zip": "",
                    "country": "CZ",
                    "ico": "",
                    "dic": "",
                }
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

        def _upsert_invoice_party(
            db: Session,
            *,
            invoice_id: int,
            role: str,
            payload: dict,
            sync_existing: bool = True,
        ) -> InvoiceParty:
            """Create or update an InvoiceParty snapshot."""

            for pending in db.new:
                if not isinstance(pending, InvoiceParty):
                    continue
                if int(getattr(pending, "invoice_id", 0) or 0) == int(invoice_id) and str(getattr(pending, "role", "") or "") == str(role):
                    if sync_existing:
                        for k, v in payload.items():
                            setattr(pending, k, v)
                    return pending

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
                for k, v in payload.items():
                    setattr(existing, k, v)
            return existing

        def _sync_invoice_parties(
            db: Session,
            *,
            invoice: Invoice,
            subject: Subject | None,
            contact: Contact | None,
            sync_existing: bool = True,
        ) -> tuple[InvoiceParty, InvoiceParty]:
            """Ensure buyer/seller snapshots exist (and optionally sync fields)."""

            seller = _upsert_invoice_party(
                db,
                invoice_id=int(invoice.id),
                role="seller",
                payload=_party_payload_from_subject(subject),
                sync_existing=sync_existing,
            )
            buyer = _upsert_invoice_party(
                db,
                invoice_id=int(invoice.id),
                role="buyer",
                payload=_party_payload_from_contact(contact),
                sync_existing=sync_existing,
            )

            # Cached buyer fields for lists.
            invoice.buyer_name_cache = buyer.name or None
            invoice.buyer_registration_no_cache = buyer.ico or None

            return buyer, seller

        PARTY_FORM_FIELDS = ("name", "email", "phone", "street", "city", "zip", "country", "ico", "dic")
        PARTY_MEANINGFUL_FORM_FIELDS = tuple(key for key in PARTY_FORM_FIELDS if key != "country")

        def _normalize_party_payload(payload: dict[str, object]) -> dict[str, str]:
            normalized: dict[str, str] = {}
            for key in PARTY_FORM_FIELDS:
                value = str(payload.get(key) or "").strip()
                if key == "country":
                    value = (value or "CZ").upper()[:2]
                normalized[key] = value
            return normalized

        def _party_payload_from_form(form, *, prefix: str, fallback: dict[str, object]) -> dict[str, str]:
            payload = dict(fallback or {})
            for key in PARTY_FORM_FIELDS:
                form_key = f"{prefix}_{key}"
                if form_key in form:
                    payload[key] = str(form.get(form_key) or "").strip()
            return _normalize_party_payload(payload)

        def _party_payload_has_meaningful_values(payload: dict[str, object] | None) -> bool:
            if not payload:
                return False
            return any(str(payload.get(key) or "").strip() for key in PARTY_MEANINGFUL_FORM_FIELDS)

        def _party_payload_from_snapshot_or_fallback(
            db: Session,
            *,
            invoice_id: int,
            role: str,
            fallback: dict[str, object],
        ) -> dict[str, str]:
            party = db.scalar(
                select(InvoiceParty).where(
                    InvoiceParty.invoice_id == int(invoice_id),
                    InvoiceParty.role == str(role),
                )
            )
            if party is None:
                return _normalize_party_payload(fallback)
            return _normalize_party_payload({key: getattr(party, key, "") for key in PARTY_FORM_FIELDS})

        def _apply_manual_invoice_parties(
            db: Session,
            *,
            invoice: Invoice,
            buyer_payload: dict[str, object],
            seller_payload: dict[str, object] | None = None,
        ) -> None:
            if seller_payload is not None:
                _upsert_invoice_party(
                    db,
                    invoice_id=int(invoice.id),
                    role="seller",
                    payload=_normalize_party_payload(seller_payload),
                    sync_existing=True,
                )
            buyer = _upsert_invoice_party(
                db,
                invoice_id=int(invoice.id),
                role="buyer",
                payload=_normalize_party_payload(buyer_payload),
                sync_existing=True,
            )
            invoice.buyer_name_cache = buyer.name or None
            invoice.buyer_registration_no_cache = buyer.ico or None

        def _load_invoice_parties_map(db: Session, *, invoice_id: int) -> dict[str, InvoiceParty]:
            parties = db.scalars(
                select(InvoiceParty).where(InvoiceParty.invoice_id == int(invoice_id))
            ).all()
            return {str(p.role): p for p in parties}

        def _rounding_line_item(invoice: Invoice | object) -> object | None:
            adjustment = int(getattr(invoice, "rounding_adjustment_cents", 0) or 0)
            if adjustment == 0:
                return None
            return type(
                "InvoiceRoundingLine",
                (),
                {
                    "id": None,
                    "description": "Zaokrouhlení",
                    "quantity": Decimal("1.00"),
                    "unit_price_cents": adjustment,
                    "vat_rate": Decimal("0.00"),
                    "line_net_cents": adjustment,
                    "line_vat_cents": 0,
                    "line_total_cents": adjustment,
                    "sort_order": 999999,
                    "is_rounding": True,
                },
            )()

        def _items_with_rounding_line(items: list[InvoiceItem] | list[object], invoice: Invoice | object) -> list[object]:
            rows = list(items or [])
            rounding_row = _rounding_line_item(invoice)
            if rounding_row is not None:
                rows.append(rounding_row)
            return rows

        def _recalc_invoice_total_cents(db: Session, *, invoice: Invoice) -> None:
            """Recalculate invoice.total_cents from items - discount + rounding."""

            items_total = db.scalar(
                select(func.coalesce(func.sum(InvoiceItem.line_total_cents), 0)).where(
                    InvoiceItem.invoice_id == int(invoice.id)
                )
            )
            invoice.total_cents = (
                int(items_total or 0)
                - int(getattr(invoice, "discount_cents", 0) or 0)
                + int(invoice.rounding_adjustment_cents or 0)
            )

        def _invoice_number_year(issue_date: date | None = None) -> int:
            try:
                return int((issue_date or date.today()).year)
            except Exception:
                return int(date.today().year)

        def _normalized_series_prefix(prefix: str | None, *, year: int) -> str:
            raw = str(prefix or "").strip()
            raw = re.sub(r"^20\d{2}[-_/\s]*", "", raw)
            raw = re.sub(r"[^A-Za-z0-9/_-]+", "-", raw).strip("-_/")
            if raw:
                return f"{int(year)}-{raw}-"
            return f"{int(year)}-"

        def _invoice_series_definition_for_type(document_type: str | None) -> tuple[str, str]:
            normalized = _normalize_invoice_document_type(document_type)
            if normalized == "quote":
                return "quote", "NAB"
            if normalized == "credit_note":
                return "credit_note", "DOB"
            if normalized == "proforma":
                return "proforma", "ZAL"
            return "default", ""

        def _invoice_source_invoice_number(db: Session, *, invoice: Invoice | None) -> str:
            if invoice is None or getattr(invoice, "source_invoice_id", None) is None:
                return ""
            try:
                source = db.scalar(
                    select(Invoice.number)
                    .where(Invoice.id == int(invoice.source_invoice_id))
                    .limit(1)
                )
            except SQLAlchemyError:
                return ""
            return str(source or "")

        def _invoice_related_credit_note_summary(
            db: Session,
            *,
            invoice_id: int,
            include_drafts: bool = False,
        ) -> dict[str, object]:
            try:
                rows = db.scalars(
                    select(Invoice)
                    .where(Invoice.source_invoice_id == int(invoice_id))
                    .where(Invoice.document_type == "credit_note")
                    .order_by(*_invoice_newest_first_ordering())
                ).all()
            except SQLAlchemyError:
                rows = []
            relevant = [
                row
                for row in rows
                if include_drafts or str(getattr(row, "status", "") or "").strip().lower() != "draft"
            ]
            credited_total_cents = abs(sum(int(getattr(row, "total_cents", 0) or 0) for row in relevant))
            return {
                "items": relevant,
                "credited_total_cents": int(credited_total_cents),
            }

        def _credit_note_available_cents(
            db: Session,
            *,
            source_invoice: Invoice,
            current_credit_note_id: int | None = None,
        ) -> int:
            summary = _invoice_related_credit_note_summary(
                db,
                invoice_id=int(source_invoice.id),
                include_drafts=False,
            )
            already_credited = int(summary.get("credited_total_cents") or 0)
            if current_credit_note_id:
                current_credit_note = next(
                    (
                        row
                        for row in list(summary.get("items") or [])
                        if int(getattr(row, "id", 0) or 0) == int(current_credit_note_id)
                    ),
                    None,
                )
                if current_credit_note is not None:
                    already_credited -= abs(int(getattr(current_credit_note, "total_cents", 0) or 0))
            return max(int(getattr(source_invoice, "total_cents", 0) or 0) - already_credited, 0)

        def _invoice_conversion_targets(document_type: str | None) -> list[tuple[str, str]]:
            normalized = _normalize_invoice_document_type(document_type)
            if normalized == "quote":
                return [("proforma", "Převést na zálohovou fakturu"), ("invoice", "Převést na ostrou fakturu")]
            if normalized == "proforma":
                return [("invoice", "Převést na ostrou fakturu")]
            return []

        def _default_conversion_notes(source_invoice: Invoice, *, target_document_type: str) -> str | None:
            source_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))
            source_number = str(getattr(source_invoice, "number", "") or "").strip()
            if not source_number:
                return None
            if source_type == "quote" and target_document_type == "proforma":
                return f"Zálohová faktura navazuje na nabídku {source_number}"
            if source_type == "quote" and target_document_type == "invoice":
                return f"Faktura navazuje na nabídku {source_number}"
            if source_type == "proforma" and target_document_type == "invoice":
                return f"Faktura navazuje na zálohovou fakturu {source_number}"
            return None

        def _clone_invoice_from_template(
            db: Session,
            *,
            source_invoice: Invoice,
            source_items: list[InvoiceItem],
            subject: Subject | None,
            issue_date: date,
            due_date: date,
            document_type: str | None = None,
            source_invoice_id: int | None = None,
            notes_override: str | None = None,
            render_tokens: bool = False,
        ) -> Invoice:
            target_document_type = _normalize_invoice_document_type(document_type or getattr(source_invoice, "document_type", "invoice"))
            source_document_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))
            sid = int(getattr(source_invoice, "subject_id", 0) or 0)
            contact = getattr(source_invoice, "contact", None)
            default_series = _get_or_create_default_invoice_series(
                db,
                subject_id=sid,
                document_type=target_document_type,
            )
            series_to_use = default_series
            if source_document_type == target_document_type and getattr(source_invoice, "series_id", None) is not None:
                source_series = db.scalar(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.id == int(source_invoice.series_id))
                    .where(InvoiceSeries.subject_id == int(sid))
                )
                if source_series is not None:
                    series_to_use = source_series

            selected_bank_account: SubjectBankAccount | None = None
            if getattr(source_invoice, "bank_account_id", None) is not None:
                selected_bank_account = db.scalar(
                    select(SubjectBankAccount)
                    .where(SubjectBankAccount.id == int(source_invoice.bank_account_id))
                    .where(SubjectBankAccount.subject_id == int(sid))
                )
            if selected_bank_account is None and str(getattr(source_invoice, "payment_method", "") or "bank_transfer") == "bank_transfer":
                selected_bank_account = _default_subject_bank_account(
                    db,
                    subject_id=sid,
                    currency=str(getattr(source_invoice, "currency", None) or "CZK"),
                )

            footer_text = str(getattr(source_invoice, "footer_text", "") or "") or None
            notes_value = (
                notes_override
                if notes_override is not None
                else (str(getattr(source_invoice, "notes", "") or "") or None)
            )
            if render_tokens:
                footer_text = _render_recurring_tokens(footer_text, issue_date=issue_date) or None
                notes_value = _render_recurring_tokens(notes_value, issue_date=issue_date) or None

            cloned_invoice = Invoice(
                subject_id=sid,
                number=f"DRAFT-{uuid4().hex[:12]}",
                status="draft",
                issue_date=issue_date,
                taxable_supply_date=issue_date,
                due_date=due_date,
                currency=str(getattr(source_invoice, "currency", None) or "CZK").upper(),
                invoice_language=_normalize_invoice_language(getattr(source_invoice, "invoice_language", None)),
                invoice_style=_normalize_invoice_style(getattr(source_invoice, "invoice_style", None)),
                variable_symbol=_contact_fixed_variable_symbol(contact) or _normalize_variable_symbol(getattr(source_invoice, "variable_symbol", None)) or None,
                notes=notes_value,
                payment_method=str(getattr(source_invoice, "payment_method", "") or "bank_transfer"),
                footer_mode=str(getattr(source_invoice, "footer_mode", "") or _default_invoice_footer_mode(subject)),
                footer_text=footer_text,
                document_type=target_document_type,
                source_invoice_id=int(source_invoice_id) if source_invoice_id is not None else None,
                contact_id=int(getattr(source_invoice, "contact_id", 0) or 0),
                buyer_name_cache=str(getattr(source_invoice, "buyer_name_cache", None) or getattr(contact, "name", "") or ""),
                buyer_registration_no_cache=str(getattr(source_invoice, "buyer_registration_no_cache", None) or getattr(contact, "ico", "") or "") or None,
                discount_cents=int(getattr(source_invoice, "discount_cents", 0) or 0),
                rounding_adjustment_cents=int(getattr(source_invoice, "rounding_adjustment_cents", 0) or 0),
                total_cents=0,
                series_id=(int(series_to_use.id) if series_to_use is not None else None),
            )
            if subject is not None:
                _maybe_ensure_invoice_public_link(db, invoice=cloned_invoice, subject=subject)
            db.add(cloned_invoice)
            db.flush()
            cloned_invoice.number = f"DRAFT-{int(cloned_invoice.id)}"

            _sync_invoice_parties(
                db,
                invoice=cloned_invoice,
                subject=subject,
                contact=contact,
                sync_existing=True,
            )

            if selected_bank_account is not None:
                _apply_invoice_bank_account_snapshot(
                    cloned_invoice,
                    account=selected_bank_account,
                    subject=subject,
                    allow_subject_fallback=True,
                )
            else:
                cloned_invoice.bank_account_id = None
                cloned_invoice.bank_account_label = getattr(source_invoice, "bank_account_label", None)
                cloned_invoice.bank_account_number = getattr(source_invoice, "bank_account_number", None)
                cloned_invoice.bank_account_iban = getattr(source_invoice, "bank_account_iban", None)
                cloned_invoice.bank_account_bic = getattr(source_invoice, "bank_account_bic", None)
                cloned_invoice.bank_account_country = getattr(source_invoice, "bank_account_country", None)

            _replace_invoice_items(
                db,
                invoice_id=int(cloned_invoice.id),
                items_payload=[
                    {
                        "description": (
                            _render_recurring_tokens(getattr(item, "description", ""), issue_date=issue_date)
                            if render_tokens
                            else str(getattr(item, "description", "") or "")
                        ),
                        "quantity": getattr(item, "quantity"),
                        "unit": _normalize_invoice_item_unit(getattr(item, "unit", "")),
                        "unit_price_cents": int(getattr(item, "unit_price_cents", 0) or 0),
                        "vat_rate": getattr(item, "vat_rate"),
                        "line_net_cents": int(getattr(item, "line_net_cents", 0) or 0),
                        "line_vat_cents": int(getattr(item, "line_vat_cents", 0) or 0),
                        "line_total_cents": int(getattr(item, "line_total_cents", 0) or 0),
                    }
                    for item in source_items
                ],
            )
            _recalc_invoice_total_cents(db, invoice=cloned_invoice)
            return cloned_invoice

        def _issue_invoice_object(
            db: Session,
            *,
            invoice: Invoice,
            subject: Subject | None,
            contact: Contact | None,
        ) -> None:
            sid = int(getattr(invoice, "subject_id", 0) or 0)
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            if getattr(invoice, "series_id", None) is not None:
                series = db.scalar(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.id == int(invoice.series_id))
                    .where(InvoiceSeries.subject_id == int(sid))
                )
            else:
                series = None
            if series is None:
                series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=document_type,
                )
                invoice.series_id = int(series.id)

            selected_bank_account: SubjectBankAccount | None = None
            if invoice.bank_account_id is not None:
                selected_bank_account = db.scalar(
                    select(SubjectBankAccount)
                    .where(SubjectBankAccount.id == int(invoice.bank_account_id))
                    .where(SubjectBankAccount.subject_id == int(sid))
                )
            elif not (invoice.bank_account_number or invoice.bank_account_iban):
                selected_bank_account = _default_subject_bank_account(db, subject_id=sid, currency=str(getattr(invoice, "currency", None) or "CZK"))

            invoice.number = _allocate_next_invoice_number(
                db,
                subject_id=sid,
                series_id=int(series.id),
                invoice_id=int(invoice.id),
                issue_date=invoice.issue_date,
            )
            if not _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)):
                invoice.variable_symbol = _contact_fixed_variable_symbol(contact) or variable_symbol_from_invoice_number(invoice.number)
            invoice.status = "issued"
            invoice.issued_at = utc_now()
            if subject is not None:
                _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)
            _sync_invoice_parties(
                db,
                invoice=invoice,
                subject=subject,
                contact=contact,
                sync_existing=False,
            )
            if selected_bank_account is not None:
                _apply_invoice_bank_account_snapshot(
                    invoice,
                    account=selected_bank_account,
                    subject=subject,
                    allow_subject_fallback=True,
                )
            elif not (invoice.bank_account_number or invoice.bank_account_iban):
                _apply_invoice_bank_account_snapshot(
                    invoice,
                    account=None,
                    subject=subject,
                    allow_subject_fallback=True,
                )
            _recalc_invoice_total_cents(db, invoice=invoice)








        def _send_invoice_email_automatically(
            request: Request,
            db: Session,
            *,
            invoice: Invoice,
            subject: Subject | None,
            recipient_override: str | None = None,
        ) -> tuple[bool, str]:
            recipients = split_recipients(recipient_override or getattr(getattr(invoice, "contact", None), "email", "") or "")
            if not recipients or not all(looks_like_email(item) for item in recipients):
                return False, "Kontakt nemá platný e-mail pro automatické odeslání."

            mail_ctx = _mail_identity_context(db, subject=subject, request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "").strip()
            signature_name = str(mail_ctx.get("signature_name") or "").strip()
            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )
            if not smtp_is_configured(smtp_cfg) or not looks_like_email(from_email):
                return False, "SMTP není připravené pro automatické odeslání."

            public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
            public_url = public_urls["view"] if public_urls else None
            total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
            body = (
                "Dobrý den,\n\n"
                f"v příloze zasílám {_invoice_document_type_label(getattr(invoice, 'document_type', 'invoice')).lower()} {invoice.number} "
                f"na částku {total_str}.\n"
                f"Splatnost: {invoice.due_date}.\n"
            )
            if public_url:
                body += f"\n{public_url}\n"
            body += f"\nS pozdravem,\n{signature_name or from_name}\n"

            pdf_bytes = _invoice_pdf_bytes_for_export(request, db, invoice=invoice)
            safe_no = re.sub(r"[^A-Za-z0-9._-]+", "_", str(invoice.number or "invoice"))
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=recipients,
                cc_emails=[],
                subject=_invoice_document_email_subject(getattr(invoice, "document_type", "invoice"), invoice.number),
                body=body,
                attachment_pdf=(f"{safe_no}.pdf", bytes(pdf_bytes)),
            )
            email_row = InvoiceEmail(
                invoice_id=int(invoice.id),
                kind="invoice",
                from_email=from_email,
                to_email=_format_recipient_log_value(to_emails=recipients),
                subject=_invoice_document_email_subject(getattr(invoice, "document_type", "invoice"), invoice.number)[:255],
                body=body,
                status="queued",
            )
            db.add(email_row)
            db.flush()
            try:
                message_id, _debug = send_via_smtp(smtp_cfg, msg)
                email_row.status = "sent"
                email_row.sent_at = utc_now()
                email_row.message_id = (message_id or "")[:255] if message_id else None
                if str(getattr(invoice, "status", "") or "").strip().lower() == "issued":
                    invoice.status = "sent"
                    invoice.sent_at = utc_now()
                return True, ""
            except Exception as exc:
                email_row.status = "error"
                logging.getLogger("fakturek").error(
                    "Automatic invoice email failed (error_type=%s)",
                    type(exc).__name__,
                )
                email_row.error_message = "E-mail se nepodařilo odeslat."
                return False, "E-mail se nepodařilo odeslat."

        def _normalize_ico_value(value: str | None) -> str:
            return re.sub(r"\D+", "", str(value or "").strip())





        def _copy_invoice_party_snapshots(
            db: Session,
            *,
            source_invoice: Invoice,
            target_invoice: Invoice,
        ) -> None:
            parties = db.scalars(
                select(InvoiceParty)
                .where(InvoiceParty.invoice_id == int(source_invoice.id))
                .order_by(InvoiceParty.id.asc())
            ).all()
            for party in parties:
                _upsert_invoice_party(
                    db,
                    invoice_id=int(target_invoice.id),
                    role=str(getattr(party, "role", "") or ""),
                    payload={key: getattr(party, key, "") for key in PARTY_FORM_FIELDS},
                    sync_existing=True,
                )
            buyer = db.scalar(
                select(InvoiceParty)
                .where(InvoiceParty.invoice_id == int(target_invoice.id))
                .where(InvoiceParty.role == "buyer")
                .limit(1)
            )
            if buyer is not None:
                target_invoice.buyer_name_cache = buyer.name or None
                target_invoice.buyer_registration_no_cache = buyer.ico or None





        def _effective_invoice_payment_context(
            db: Session,
            *,
            invoice: Invoice,
            language: str | None = None,
        ) -> tuple[str, str]:
            payment_method = str(getattr(invoice, "payment_method", "") or "bank_transfer").strip().lower() or "bank_transfer"
            return payment_method, _invoice_payment_method_label(payment_method, language)







        def _get_or_create_default_invoice_series(
            db: Session,
            *,
            subject_id: int,
            document_type: str | None = None,
        ) -> InvoiceSeries:
            """Return the default numbering series for a given invoice document type."""

            series_name, series_prefix = _invoice_series_definition_for_type(document_type)
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
                last_counter_year=_invoice_number_year(),
            )
            db.add(series)
            try:
                db.flush()
            except SQLAlchemyError:
                db.rollback()
                series = db.scalar(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.subject_id == int(subject_id))
                    .where(InvoiceSeries.name == str(series_name))
                )
                if series is None:
                    raise
            return series

        def _split_invoice_number_prefix_counter(number: str | None) -> tuple[str, int, int] | None:
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

        def _observed_series_counter_for_year(
            db: Session,
            *,
            subject_id: int,
            series: InvoiceSeries | None,
            year: int,
        ) -> int:
            if series is None:
                return 0
            prefix = _normalized_series_prefix(getattr(series, "prefix", None), year=int(year))
            rows = db.scalars(
                select(Invoice.number)
                .where(Invoice.subject_id == int(subject_id))
                .where(Invoice.number.is_not(None))
                .where(Invoice.number.startswith(prefix))
            ).all()
            max_counter = 0
            for raw_number in rows:
                parts = _split_invoice_number_prefix_counter(raw_number)
                if parts is None:
                    continue
                number_prefix, counter, _digits_len = parts
                if str(number_prefix) != str(prefix):
                    continue
                if int(counter) > int(max_counter):
                    max_counter = int(counter)
            return int(max_counter)

        def _sync_series_counter_for_year(
            db: Session,
            *,
            subject_id: int,
            series: InvoiceSeries | None,
            year: int,
        ) -> int:
            if series is None:
                return 0
            observed = _observed_series_counter_for_year(
                db,
                subject_id=int(subject_id),
                series=series,
                year=int(year),
            )
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

        def _sync_series_list_for_year(
            db: Session,
            *,
            subject_id: int,
            series_list: list[InvoiceSeries] | None,
            year: int,
        ) -> None:
            for series in list(series_list or []):
                _sync_series_counter_for_year(
                    db,
                    subject_id=int(subject_id),
                    series=series,
                    year=int(year),
                )

        def _format_invoice_number(series: InvoiceSeries, counter: int, *, year: int | None = None) -> str:
            number_year = int(year or _invoice_number_year())
            prefix = _normalized_series_prefix(series.prefix, year=number_year)
            pad = max(1, min(int(series.pad_length or 0), 20))
            digits = str(int(counter)).zfill(pad)
            number = f"{prefix}{digits}"
            if len(number) > 50:
                raise ValueError("Číslo faktury je příliš dlouhé pro DB sloupec (max 50).")
            return number

        def _series_next_number_preview(series: InvoiceSeries | None, *, year: int | None = None) -> str:
            if series is None:
                return "Přidělí se při vystavení"
            number_year = int(year or _invoice_number_year())
            try:
                last_year = int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None
            except Exception:
                last_year = None
            next_counter = 1 if last_year != number_year else int(series.last_counter or 0) + 1
            try:
                return _format_invoice_number(series, next_counter, year=number_year)
            except Exception:
                return "Přidělí se při vystavení"

        def _build_invoice_series_options(
            series_list: list[InvoiceSeries] | None,
            *,
            year: int | None = None,
        ) -> list[dict[str, object]]:
            options: list[dict[str, object]] = []
            for series in list(series_list or []):
                options.append(
                    {
                        "id": int(series.id),
                        "name": str(series.name or ""),
                        "next_number_preview": _series_next_number_preview(series, year=year),
                        "prefix": str(series.prefix or ""),
                        "pad_length": int(series.pad_length or 4),
                        "last_counter": int(series.last_counter or 0),
                        "last_counter_year": int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None,
                    }
                )
            return options

        def _pick_invoice_series_for_preview(
            series_list: list[InvoiceSeries] | None,
            *,
            selected_id: int | None,
            default_series: InvoiceSeries | None,
        ) -> InvoiceSeries | None:
            if selected_id is not None:
                for series in list(series_list or []):
                    try:
                        if int(series.id) == int(selected_id):
                            return series
                    except Exception:
                        continue
            return default_series

        def _invoice_has_vat(items: list[InvoiceItem] | list[object]) -> bool:
            for item in list(items or []):
                try:
                    rate = Decimal(str(getattr(item, "vat_rate", "0") or "0"))
                except Exception:
                    raw = str(getattr(item, "vat_rate", "") or "").strip()
                    if raw not in {"", "0", "0.0", "0.00"}:
                        return True
                    continue
                if rate != Decimal("0"):
                    return True
            return False

        def _invoice_vat_summary(items: list[InvoiceItem] | list[object]) -> dict[str, object]:
            buckets: dict[str, dict[str, object]] = {}
            total_net = 0
            total_vat = 0
            total_gross = 0
            for item in list(items or []):
                try:
                    rate = Decimal(str(getattr(item, "vat_rate", "0") or "0")).quantize(Decimal("0.01"))
                except Exception:
                    rate = Decimal("0.00")
                key = f"{rate:.2f}"
                bucket = buckets.setdefault(key, {"rate": rate, "net_cents": 0, "vat_cents": 0, "gross_cents": 0})
                net = int(getattr(item, "line_net_cents", 0) or 0)
                vat = int(getattr(item, "line_vat_cents", 0) or 0)
                gross = int(getattr(item, "line_total_cents", 0) or 0)
                bucket["net_cents"] = int(bucket["net_cents"]) + net
                bucket["vat_cents"] = int(bucket["vat_cents"]) + vat
                bucket["gross_cents"] = int(bucket["gross_cents"]) + gross
                total_net += net
                total_vat += vat
                total_gross += gross
            rows = sorted(buckets.values(), key=lambda row: Decimal(str(row["rate"])), reverse=True)
            return {"rows": rows, "net_cents": total_net, "vat_cents": total_vat, "gross_cents": total_gross}

        def _invoice_amount_for_vat_view(invoice: Invoice, vat_view: str) -> int:
            view = str(vat_view or "gross").strip().lower()
            items = list(getattr(invoice, "items", []) or [])
            if view == "net":
                return sum(int(getattr(item, "line_net_cents", 0) or 0) for item in items)
            if view == "vat":
                return sum(int(getattr(item, "line_vat_cents", 0) or 0) for item in items)
            return int(getattr(invoice, "total_cents", 0) or 0)

        def _invoice_vat_classification(*, invoice: Invoice, subject: Subject | None, vat_summary: dict[str, object]) -> dict[str, str]:
            if not bool(getattr(subject, "is_vat_payer", False)):
                return {"mode": "Neplátce DPH", "vat_return": "mimo přiznání k DPH", "control_statement": "mimo kontrolní hlášení", "summary_statement": "mimo souhrnné hlášení"}
            contact = getattr(invoice, "contact", None)
            buyer_country = str(getattr(contact, "country", "") or "CZ").strip().upper() or "CZ"
            buyer_dic = str(getattr(contact, "dic", "") or "").strip().upper()
            vat_cents = int(vat_summary.get("vat_cents") or 0)
            total_cents = int(getattr(invoice, "total_cents", 0) or 0)
            if buyer_country != "CZ" and buyer_dic and vat_cents == 0:
                return {"mode": "Přenesená daňová povinnost / reverse charge", "vat_return": "ověř řádek podle typu plnění", "control_statement": "mimo tuzemské KH", "summary_statement": "pravděpodobně souhrnné hlášení"}
            if vat_cents > 0:
                control = "oddíl A.4" if buyer_dic and total_cents > 1000000 else "oddíl A.5"
                return {"mode": "Tuzemské zdanitelné plnění", "vat_return": "řádek 1", "control_statement": control, "summary_statement": "není součástí"}
            return {"mode": "Plátce DPH, sazba 0 %", "vat_return": "zkontroluj režim plnění", "control_statement": "podle režimu plnění", "summary_statement": "podle země a typu plnění"}

        def _allocate_next_invoice_number(
            db: Session,
            *,
            subject_id: int,
            series_id: int,
            invoice_id: int,
            issue_date: date | None = None,
        ) -> str:
            """Allocate the next invoice number transactionally."""

            series = db.scalar(
                select(InvoiceSeries)
                .where(InvoiceSeries.id == int(series_id))
                .where(InvoiceSeries.subject_id == int(subject_id))
                .with_for_update()
            )
            if series is None:
                raise ValueError("Číselná řada neexistuje.")

            number_year = _invoice_number_year(issue_date)
            _sync_series_counter_for_year(
                db,
                subject_id=int(subject_id),
                series=series,
                year=int(number_year),
            )
            try:
                last_year = int(series.last_counter_year) if getattr(series, "last_counter_year", None) else None
            except Exception:
                last_year = None
            base_counter = 0 if last_year != number_year else int(series.last_counter or 0)

            for offset in range(1, 1001):
                next_counter = int(base_counter) + offset
                candidate = _format_invoice_number(series, next_counter, year=number_year)
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

            raise ValueError("Nepodařilo se vybrat unikátní číslo faktury (příliš mnoho kolizí).")

    if _db_enabled:





































































































        @app.get("/settings", response_class=HTMLResponse)
        def settings_page(
            request: Request,
            db: Session = Depends(get_db),
            saved: bool = False,
            edit_account: int | None = None,
            info: str | None = None,
            error: str | None = None,
        ):
            """Settings page.

            In DB-enabled mode we allow editing the issuer (seller) profile.

            Master plan stores this data in the default Subject (id=1).
            We still read legacy `issuer_profiles` as a fallback.
            """

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_sid = _current_subject_id()
            bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
            current_user = _current_user_settings_view(db, request)
            current_user_id = int(current_user.get("id") or 0) if current_user.get("id") else None
            edit_account_row = None
            if edit_account is not None:
                edit_account_row = db.scalar(
                    select(SubjectBankAccount)
                    .where(SubjectBankAccount.id == int(edit_account))
                    .where(SubjectBankAccount.subject_id == int(current_sid))
                )
            api_token_created = _pop_api_token_created_flash(request, user_id=current_user_id)
            return _render_settings_page(
                request,
                issuer=issuer,
                issuer_source=issuer_source,
                saved=bool(saved),
                info=info,
                error=error,
                setup_warnings=_subject_setup_warnings(db, subject=subject, require_bank_account=True),
                issued_pdf_refresh_count=_count_refreshable_issued_invoices(db, subject_id=int(current_sid)),
                current_user=current_user,
                bank_accounts=bank_accounts,
                api_tokens=_api_tokens_view_rows(db, user_id=int(current_user_id)) if current_user_id is not None else [],
                api_token_created=api_token_created,
                account_deletion_summary=(
                    _account_deletion_summary(db, user_id=int(current_user_id))
                    if current_user_id is not None
                    else {}
                ),
                account_prefill=(
                    _account_prefill_from_row(
                        edit_account_row,
                        fallback_country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                    )
                    if edit_account_row is not None
                    else _blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    )
                ),
                **_settings_subject_admin_context(
                    db,
                    request=request,
                    issuer=issuer,
                    subject=subject,
                    subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                    user_access_prefill=_blank_user_access_prefill(),
                ),
            )


        def _subject_settings_template_context(
            request: Request,
            db: Session,
            *,
            info: str | None = None,
            error: str | None = None,
            subject_prefill: dict | None = None,
            user_access_prefill: dict | None = None,
            existing_user_link_prefill: dict | None = None,
        ) -> dict[str, object]:
            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_user = _current_user_settings_view(db, request)
            context = {
                "app_env": settings.app_env,
                "debug": settings.debug,
                "issuer": issuer,
                "issuer_source": issuer_source,
                "info": info,
                "error": error,
                "current_user": current_user,
                "currency_options": _build_currency_options(
                    getattr(subject, "default_currency", None) or issuer.get("default_currency")
                ),
            }
            context.update(
                _settings_subject_admin_context(
                    db,
                    request=request,
                    issuer=issuer,
                    subject=subject,
                    subject_prefill=subject_prefill or _blank_subject_prefill(issuer=issuer, subject=subject),
                    user_access_prefill=user_access_prefill or _blank_user_access_prefill(),
                    existing_user_link_prefill=existing_user_link_prefill or _blank_existing_user_link_prefill(),
                )
            )
            return context

        @app.get("/settings/subjects/new", response_class=HTMLResponse)
        def settings_subject_new_page(
            request: Request,
            db: Session = Depends(get_db),
            info: str | None = None,
            error: str | None = None,
        ):
            if _current_user_id_or_none(request) is None:
                return RedirectResponse(url=f"/login?next={quote('/settings/subjects/new', safe='')}", status_code=303)
            return templates.TemplateResponse(
                request,
                "settings/subjects_new.html",
                _subject_settings_template_context(request, db, info=info, error=error),
            )

        @app.get("/settings/subjects/access", response_class=HTMLResponse)
        def settings_subject_access_page(
            request: Request,
            db: Session = Depends(get_db),
            info: str | None = None,
            error: str | None = None,
        ):
            if _current_user_id_or_none(request) is None:
                return RedirectResponse(url=f"/login?next={quote('/settings/subjects/access', safe='')}", status_code=303)
            return templates.TemplateResponse(
                request,
                "settings/subjects_access.html",
                _subject_settings_template_context(request, db, info=info, error=error),
            )

        def _account_deletion_summary(db: Session, *, user_id: int) -> dict[str, object]:
            links = db.scalars(
                select(UserSubject)
                .where(UserSubject.user_id == int(user_id))
                .order_by(UserSubject.subject_id.asc())
            ).all()
            subject_ids = [int(link.subject_id) for link in links]
            owned_subject_ids = [
                int(link.subject_id)
                for link in links
                if str(getattr(link, "role", "") or "").strip().lower() == "owner"
            ]
            sole_owned_subject_ids: list[int] = []
            for subject_id in owned_subject_ids:
                other_owner = db.scalar(
                    select(UserSubject)
                    .where(UserSubject.subject_id == int(subject_id))
                    .where(UserSubject.user_id != int(user_id))
                    .where(UserSubject.role == "owner")
                    .limit(1)
                )
                if other_owner is None:
                    sole_owned_subject_ids.append(int(subject_id))

            invoice_count = 0
            issued_invoice_count = 0
            if subject_ids:
                invoice_count = int(
                    db.scalar(select(func.count(Invoice.id)).where(Invoice.subject_id.in_(subject_ids))) or 0
                )
                issued_invoice_count = int(
                    db.scalar(
                        select(func.count(Invoice.id))
                        .where(Invoice.subject_id.in_(subject_ids))
                        .where(Invoice.status.notin_(["draft"]))
                    )
                    or 0
                )
            api_token_count = int(
                db.scalar(
                    select(func.count(ApiToken.id))
                    .where(ApiToken.user_id == int(user_id))
                    .where(ApiToken.revoked_at.is_(None))
                )
                or 0
            )
            return {
                "subject_count": len(subject_ids),
                "owned_subject_count": len(owned_subject_ids),
                "sole_owned_subject_count": len(sole_owned_subject_ids),
                "sole_owned_subject_ids": sole_owned_subject_ids,
                "invoice_count": invoice_count,
                "issued_invoice_count": issued_invoice_count,
                "api_token_count": api_token_count,
            }

        def _send_account_deletion_notification(
            request: Request,
            db: Session,
            *,
            user: User,
            summary: dict[str, object],
            reason: str,
            scheduled_for: datetime,
        ) -> tuple[bool, str]:
            recipient = str(settings.smtp_from_email or "").strip()
            mail_ctx = _mail_identity_context(db, subject=_load_subject_for_current_session(db), request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "Fakturek.cz").strip()
            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )
            if (
                not recipient
                or not looks_like_email(recipient)
                or not smtp_is_configured(smtp_cfg)
                or not looks_like_email(from_email)
            ):
                return False, "SMTP není připravené pro notifikaci."

            username = str(getattr(user, "username", "") or "")
            user_email = str(getattr(user, "email", "") or "")
            reason_text = str(reason or "").strip() or "Neuvedeno"
            body = (
                "Ahoj,\n\n"
                "uživatel požádal ve Fakturku o zrušení účtu.\n\n"
                f"Uživatel: {username}\n"
                f"E-mail: {user_email}\n"
                f"ID uživatele: {int(getattr(user, 'id', 0) or 0)}\n"
                f"Naplánované hard-delete: {scheduled_for.strftime('%d.%m.%Y %H:%M')}\n\n"
                f"Subjekty s přístupem: {int(summary.get('subject_count') or 0)}\n"
                f"Subjekty bez jiného vlastníka: {int(summary.get('sole_owned_subject_count') or 0)}\n"
                f"Faktury a doklady: {int(summary.get('invoice_count') or 0)}\n"
                f"Vystavené doklady: {int(summary.get('issued_invoice_count') or 0)}\n"
                f"Aktivní API klíče: {int(summary.get('api_token_count') or 0)}\n\n"
                f"Důvod:\n{reason_text}\n"
            )
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_emails=[recipient],
                cc_emails=[],
                subject=f"Fakturek: zrušení účtu {username or user_email}",
                body=body,
            )
            try:
                send_via_smtp(smtp_cfg, msg)
                return True, ""
            except Exception as exc:
                logging.getLogger("fakturek").error(
                    "Account deletion notification failed (error_type=%s)",
                    type(exc).__name__,
                )
                return False, "Notifikaci se nepodařilo odeslat."

        def _account_delete_confirmation_phrase(request: Request) -> str:
            return "DELETE ACCOUNT" if _normalize_ui_language(request.session.get("ui_language")) == "en" else "SMAZAT ÚČET"

        def _account_delete_confirmation_allowed(phrase: str) -> bool:
            normalized = str(phrase or "").strip()
            return normalized in {"SMAZAT ÚČET", "DELETE ACCOUNT"}

        @app.get("/settings/account/delete", response_class=HTMLResponse)
        def settings_account_delete_page(request: Request, db: Session = Depends(get_db)):
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return RedirectResponse(url="/login?next=%2Fsettings%2Faccount%2Fdelete", status_code=303)
            user = db.get(User, int(user_id))
            if user is None:
                return RedirectResponse(url="/login", status_code=303)
            summary = _account_deletion_summary(db, user_id=int(user.id))
            return templates.TemplateResponse(
                request,
                "settings/account_delete.html",
                {
                    "current_user": _current_user_settings_view(db, request),
                    "summary": summary,
                    "confirmation_phrase": _account_delete_confirmation_phrase(request),
                    "deletion_pending": getattr(user, "deletion_requested_at", None) is not None,
                    "error": "",
                },
            )

        @app.post("/settings/account/delete")
        async def settings_account_delete_submit(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return RedirectResponse(url="/login?next=%2Fsettings%2Faccount%2Fdelete", status_code=303)
            user = db.get(User, int(user_id))
            if user is None:
                return RedirectResponse(url="/login", status_code=303)
            summary = _account_deletion_summary(db, user_id=int(user.id))

            def _render_error(message: str, *, status_code: int = 400) -> HTMLResponse:
                return templates.TemplateResponse(
                    request,
                    "settings/account_delete.html",
                    {
                        "current_user": _current_user_settings_view(db, request),
                        "summary": summary,
                        "confirmation_phrase": _account_delete_confirmation_phrase(request),
                        "deletion_pending": getattr(user, "deletion_requested_at", None) is not None,
                        "error": message,
                    },
                    status_code=status_code,
                )

            if getattr(user, "deletion_requested_at", None) is not None:
                return _render_error("Zrušení účtu už je naplánované.", status_code=400)
            password = str(form.get("password") or "")
            phrase = str(form.get("confirmation") or "").strip()
            reason = str(form.get("reason") or "").strip()
            if not verify_password(password, str(getattr(user, "password_hash", "") or "")):
                return _render_error("Heslo nesedí.")
            if not _account_delete_confirmation_allowed(phrase):
                return _render_error(f"Pro potvrzení opiš přesně text {_account_delete_confirmation_phrase(request)}.")

            now = utc_now()
            scheduled_for = now + timedelta(days=14)
            sole_owned_subject_ids = [int(value) for value in summary.get("sole_owned_subject_ids", [])]

            try:
                user.is_active = False
                user.deletion_requested_at = now
                user.deletion_scheduled_for = scheduled_for
                user.deletion_reason = reason[:1000] or None
                db.add(user)

                api_tokens = db.scalars(
                    select(ApiToken)
                    .where(ApiToken.user_id == int(user.id))
                    .where(ApiToken.revoked_at.is_(None))
                ).all()
                for token in api_tokens:
                    token.revoked_at = now
                    db.add(token)

                disabled_sync_count = 0
                if sole_owned_subject_ids:
                    bank_accounts = db.scalars(
                        select(SubjectBankAccount).where(SubjectBankAccount.subject_id.in_(sole_owned_subject_ids))
                    ).all()
                    for account in bank_accounts:
                        if bool(getattr(account, "payment_sync_enabled", False)):
                            disabled_sync_count += 1
                        account.payment_sync_enabled = False
                        account.payment_sync_last_error = "Účet vlastníka byl zrušen."
                        db.add(account)

                _audit_log(
                    db,
                    request=request,
                    action="user_account_deletion_requested",
                    entity_type="user",
                    entity_id=int(user.id),
                    user_id=int(user.id),
                    subject_id=None,
                    data={
                        "scheduled_for": scheduled_for.isoformat(timespec="seconds"),
                        "subject_count": int(summary.get("subject_count") or 0),
                        "sole_owned_subject_count": len(sole_owned_subject_ids),
                        "invoice_count": int(summary.get("invoice_count") or 0),
                        "issued_invoice_count": int(summary.get("issued_invoice_count") or 0),
                        "revoked_api_tokens": len(api_tokens),
                        "disabled_bank_sync_accounts": disabled_sync_count,
                        "reason_present": bool(reason),
                    },
                )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_error(
                    _safe_operation_error(exc, fallback="Zrušení účtu se nepodařilo uložit."),
                    status_code=500,
                )

            _send_account_deletion_notification(
                request,
                db,
                user=user,
                summary=summary,
                reason=reason,
                scheduled_for=scheduled_for,
            )
            request.session.clear()
            return RedirectResponse(url="/login?info=account-deletion-requested", status_code=303)

        @app.post("/settings/password")
        async def settings_password_update(request: Request, db: Session = Depends(get_db)):
            form = await request.form()

            current_password = str(form.get("current_password") or "")
            new_password = str(form.get("new_password") or "")
            new_password2 = str(form.get("new_password2") or "")
            password_prefill = {
                "current_password": "",
                "new_password": "",
                "new_password2": "",
            }

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_sid = _current_subject_id()
            bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
            settings_admin_context = _settings_subject_admin_context(
                db,
                request=request,
                issuer=issuer,
                subject=subject,
                subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                user_access_prefill=_blank_user_access_prefill(),
            )
            current_user = _current_user_settings_view(db, request)
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None

            if user is None:
                return RedirectResponse(url="/login?next=%2Fsettings%23security", status_code=303)

            if not current_password or not new_password or not new_password2:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="Vyplň současné heslo, nové heslo a potvrzení.",
                    status_code=400,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=not bool(bank_accounts),
                    ),
                    password_prefill=password_prefill,
                    **settings_admin_context,
                )

            if not verify_password(current_password, str(user.password_hash or "")):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="Současné heslo nesedí.",
                    status_code=400,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=not bool(bank_accounts),
                    ),
                    password_prefill=password_prefill,
                    **settings_admin_context,
                )

            password_error = new_password_length_error(new_password)
            if password_error:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=password_error,
                    status_code=400,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=not bool(bank_accounts),
                    ),
                    password_prefill=password_prefill,
                    **settings_admin_context,
                )

            if new_password != new_password2:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="Nová hesla se neshodují.",
                    status_code=400,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=not bool(bank_accounts),
                    ),
                    password_prefill=password_prefill,
                    **settings_admin_context,
                )

            try:
                user.password_hash = hash_password(new_password)
                user.session_version = int(getattr(user, "session_version", 1) or 1) + 1
                db.commit()
                request.session["session_version"] = int(user.session_version)
                request.session["authenticated_at"] = utc_now().isoformat()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit nové heslo."),
                    status_code=500,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=not bool(bank_accounts),
                    ),
                    password_prefill=password_prefill,
                    **settings_admin_context,
                )

            return RedirectResponse(url="/settings?saved=1#security", status_code=303)

        @app.post("/settings/session")
        async def settings_session_update(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None
            if user is None:
                return RedirectResponse(url="/login?next=%2Fsettings%23security", status_code=303)

            selected_days = _normalize_session_max_age_days(form.get("session_max_age_days"))
            try:
                user.session_max_age_days = int(selected_days)
                db.add(user)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                issuer, issuer_source = _load_issuer_for_current_subject(db)
                subject = _load_subject_for_current_session(db)
                current_sid = _current_subject_id()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=_current_user_settings_view(db, request),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit platnost přihlášení."),
                    status_code=500,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=current_sid),
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        is_default=False,
                    ),
                )

            return RedirectResponse(url="/settings?saved=1#security", status_code=303)

        @app.post("/settings/theme")
        async def settings_theme_update(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            selected_theme = _normalize_ui_theme(form.get("ui_theme"))

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_sid = _current_subject_id()
            bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
            settings_admin_context = _settings_subject_admin_context(
                db,
                request=request,
                issuer=issuer,
                subject=subject,
                subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                user_access_prefill=_blank_user_access_prefill(),
            )
            current_user = _current_user_settings_view(db, request)
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None

            if user is None:
                return RedirectResponse(url="/login?next=%2Fsettings%23appearance", status_code=303)

            try:
                user.ui_theme = selected_theme
                db.commit()
                request.session["ui_theme"] = selected_theme
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit vzhled."),
                    status_code=500,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            return RedirectResponse(
                url=_safe_next_url(form.get("next"), "/settings?saved=1#appearance"),
                status_code=303,
            )

        @app.post("/settings/language")
        async def settings_language_update(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            selected_language = _normalize_ui_language(form.get("ui_language"))
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None

            # In DB-backed mode the language is a user preference, not a subject
            # preference. Keeping it on the login account makes it work across all
            # organizations the user can switch between.
            if user is None:
                request.session["ui_language"] = selected_language
                return RedirectResponse(
                    url=_safe_next_url(form.get("next"), "/settings?saved=1#appearance"),
                    status_code=303,
                )

            try:
                user.ui_language = selected_language
                db.add(user)
                db.commit()
                request.session["ui_language"] = selected_language
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                issuer, issuer_source = _load_issuer_for_current_subject(db)
                subject = _load_subject_for_current_session(db)
                current_sid = _current_subject_id()
                bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=_current_user_settings_view(db, request),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit jazyk."),
                    status_code=500,
                    bank_accounts=bank_accounts,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=subject,
                        subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                        user_access_prefill=_blank_user_access_prefill(),
                    ),
                )

            return RedirectResponse(
                url=_safe_next_url(form.get("next"), "/settings?saved=1#appearance"),
                status_code=303,
            )

        @app.post("/settings/api-tokens/create")
        async def settings_api_token_create(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            token_name = str(form.get("name") or "").strip()
            expires_in_days = str(form.get("expires_in_days") or "0").strip()
            selected_subject_id_raw = str(form.get("subject_id") or "").strip()

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_sid = _current_subject_id()
            bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
            settings_admin_context = _settings_subject_admin_context(
                db,
                request=request,
                issuer=issuer,
                subject=subject,
                subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                user_access_prefill=_blank_user_access_prefill(),
            )
            current_user = _current_user_settings_view(db, request)
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None
            existing_tokens = _api_tokens_view_rows(db, user_id=int(user_id)) if user_id is not None else []
            token_prefill = {
                "name": token_name,
                "expires_in_days": expires_in_days or "0",
                "subject_id": selected_subject_id_raw,
                "can_write": bool(form.get("can_write")),
                "can_issue": bool(form.get("can_issue")),
                "can_export": bool(form.get("can_export")),
                "is_sandbox": bool(form.get("is_sandbox")),
            }

            if user is None:
                return RedirectResponse(url="/login?next=%2Fsettings%23api-access", status_code=303)

            if not token_name:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="Zadej prosím název API klíče.",
                    status_code=400,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    api_token_prefill=token_prefill,
                    active_settings_panel="api",
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            try:
                selected_subject_id = int(selected_subject_id_raw)
            except (TypeError, ValueError):
                selected_subject_id = 0

            if selected_subject_id <= 0 or not _user_can_view_subject(db, user_id=int(user.id), subject_id=selected_subject_id):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="Vyber prosím konkrétní subjekt / IČO, pro který se má API klíč vytvořit.",
                    status_code=400,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    api_token_prefill=token_prefill,
                    active_settings_panel="api",
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            valid_expiry_values = {value for value, _label in API_TOKEN_EXPIRY_OPTIONS}
            if expires_in_days not in valid_expiry_values:
                expires_in_days = "0"

            expiry_days_int = int(expires_in_days or "0")
            expires_at = utc_now() + timedelta(days=expiry_days_int) if expiry_days_int > 0 else None

            try:
                _row, plain_token = create_personal_api_token(
                    db,
                    user_id=int(user.id),
                    subject_id=int(selected_subject_id),
                    name=token_name,
                    expires_at=expires_at,
                    can_read=True,
                    can_write=bool(form.get("can_write")),
                    can_issue=bool(form.get("can_issue")),
                    can_export=bool(form.get("can_export")),
                    is_sandbox=bool(form.get("is_sandbox")),
                )
                db.commit()
            except ValueError as exc:
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vytvořit API klíč."),
                    status_code=400,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    api_token_prefill=token_prefill,
                    active_settings_panel="api",
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vytvořit API klíč."),
                    status_code=500,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    api_token_prefill=token_prefill,
                    active_settings_panel="api",
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            _store_api_token_created_flash(
                request,
                user_id=int(user.id),
                payload={
                    "name": token_name,
                    "token": plain_token,
                    "subject_id": str(selected_subject_id),
                    "subject_label": next(
                        (
                            f"{item['name']}{' • IČO ' + item['ico'] if item.get('ico') else ''}"
                            for item in settings_admin_context.get("accessible_subjects", [])
                            if int(item.get("id") or 0) == int(selected_subject_id)
                        ),
                        str(selected_subject_id),
                    ),
                    "is_sandbox": "1" if bool(form.get("is_sandbox")) else "",
                },
            )
            return RedirectResponse(url="/settings?saved=1#api-access", status_code=303)

        @app.post("/settings/api-tokens/{token_id}/revoke")
        async def settings_api_token_revoke(token_id: int, request: Request, db: Session = Depends(get_db)):
            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            current_sid = _current_subject_id()
            bank_accounts = _bank_accounts_view_rows(db, subject_id=current_sid)
            settings_admin_context = _settings_subject_admin_context(
                db,
                request=request,
                issuer=issuer,
                subject=subject,
                subject_prefill=_blank_subject_prefill(issuer=issuer, subject=subject),
                user_access_prefill=_blank_user_access_prefill(),
            )
            current_user = _current_user_settings_view(db, request)
            user_id = _current_user_id_or_none(request)
            user = db.get(User, int(user_id)) if user_id is not None else None
            existing_tokens = _api_tokens_view_rows(db, user_id=int(user_id)) if user_id is not None else []

            if user is None:
                return RedirectResponse(url="/login?next=%2Fsettings%23api-access", status_code=303)

            token = db.scalar(
                select(ApiToken)
                .where(ApiToken.id == int(token_id))
                .where(ApiToken.user_id == int(user.id))
            )
            if token is None:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error="API klíč nebyl nalezen.",
                    status_code=404,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            try:
                token.revoked_at = utc_now()
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    current_user=current_user,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se API klíč zneplatnit."),
                    status_code=500,
                    bank_accounts=bank_accounts,
                    api_tokens=existing_tokens,
                    account_prefill=_blank_account_prefill(
                        country=(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                        currency=(getattr(subject, "default_currency", None) or issuer.get("default_currency") or "CZK"),
                        is_default=not bool(bank_accounts),
                    ),
                    **settings_admin_context,
                )

            return RedirectResponse(url="/settings?saved=1#api-access", status_code=303)

        @app.post("/settings/issuer")
        async def settings_issuer_update(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            current_user = _current_user_settings_view(db, request)

            issuer = {
                "name": (form.get("name") or "").strip(),
                "email": (form.get("email") or "").strip(),
                "phone": (form.get("phone") or "").strip(),
                "street": (form.get("street") or "").strip(),
                "city": (form.get("city") or "").strip(),
                "zip": (form.get("zip") or "").strip(),
                "country": (form.get("country") or "").strip(),
                "ico": (form.get("ico") or "").strip(),
                "dic": (form.get("dic") or "").strip(),
                "is_vat_payer": bool(form.get("is_vat_payer")),
                "is_vat_identified_person": bool(form.get("is_vat_identified_person")),
                "legal_form": _normalize_subject_legal_form(form.get("legal_form")),
                "tax_regime": _normalize_tax_regime(form.get("tax_regime")),
                "flat_tax_band": _normalize_flat_tax_band(form.get("flat_tax_band")),
                "flat_tax_income_profile": _normalize_flat_tax_income_profile(form.get("flat_tax_income_profile")),
                "tax_alerts_enabled": bool(form.get("tax_alerts_enabled")),
                "tax_alert_email": (form.get("tax_alert_email") or "").strip(),
                "default_currency": (form.get("default_currency") or "CZK").strip().upper(),
                "default_invoice_style": _normalize_invoice_style(pdf_theme_to_invoice_style(form.get("invoice_pdf_theme") or form.get("default_invoice_style"))),
                "invoice_pdf_theme": normalize_invoice_pdf_theme(form.get("invoice_pdf_theme")),
                "default_invoice_footer_mode": (form.get("default_invoice_footer_mode") or "").strip().lower(),
                "default_invoice_footer_text": (form.get("default_invoice_footer_text") or "").strip(),
            }
            if not _subject_uses_business_tax_limits(issuer["legal_form"]):
                issuer["tax_regime"] = "standard"
                issuer["flat_tax_band"] = "1"
                issuer["flat_tax_income_profile"] = "general"

            if issuer["country"]:
                issuer["country"] = issuer["country"].upper()
                if len(issuer["country"]) != 2:
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source="form",
                        error="Kód země musí mít 2 znaky (např. CZ).",
                        status_code=400,
                        current_user=current_user,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    )
            else:
                issuer["country"] = "CZ"

            if issuer["default_currency"]:
                if len(issuer["default_currency"]) != 3:
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source="form",
                        error="Měna musí mít 3 znaky (např. CZK).",
                        status_code=400,
                        current_user=current_user,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    )
            else:
                issuer["default_currency"] = "CZK"

            if issuer["is_vat_payer"] and issuer["is_vat_identified_person"]:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="Plátce DPH nemůže být současně vedený jako identifikovaná osoba. Vyber jen jeden režim.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            if issuer["is_vat_payer"] and issuer["tax_regime"] == "flat":
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="Plátce DPH nemůže být současně v paušálním režimu. Přepni buď plátcovství DPH, nebo režim zdanění.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            if issuer["tax_alert_email"] and not looks_like_email(str(issuer["tax_alert_email"])):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="E-mail pro daňová upozornění není platný.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            fallback_alert_email = str(issuer["tax_alert_email"] or current_user.get("email") or issuer["email"] or "").strip()
            if issuer["tax_alerts_enabled"] and not looks_like_email(fallback_alert_email):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="Pro daňová upozornění potřebuješ platný e-mail subjektu, účtu nebo vlastní adresu pro upozornění.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            if issuer["default_invoice_footer_mode"] not in {value for value, _label in INVOICE_FOOTER_PRESET_OPTIONS}:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="Neplatná výchozí patička faktury.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            if issuer["invoice_pdf_theme"] not in {value for value, _label in INVOICE_PDF_THEME_OPTIONS}:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source="form",
                    error="Neplatný výchozí vzhled PDF faktury.",
                    status_code=400,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            if issuer["default_invoice_footer_mode"] == "custom":
                if not issuer["default_invoice_footer_text"]:
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source="form",
                        error="Pro vlastní výchozí patičku vyplň i text.",
                        status_code=400,
                        current_user=current_user,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    )
            else:
                issuer["default_invoice_footer_text"] = _invoice_footer_text_for_mode(
                    issuer["default_invoice_footer_mode"],
                    subject=None,
                )

            try:
                subject = db.get(Subject, _current_subject_id())
                if subject is None:
                    subject = Subject(id=_current_subject_id())
                    db.add(subject)
                _apply_issuer_to_subject(subject, {**issuer, "bank_account": subject.bank_account})

                accounts = _list_subject_bank_accounts(db, subject_id=int(subject.id))
                _sync_subject_legacy_bank_account(subject, accounts)

                profile = db.scalar(select(IssuerProfile).order_by(IssuerProfile.id.asc()).limit(1))
                if profile is None:
                    profile = IssuerProfile()
                    db.add(profile)
                profile.name = issuer["name"]
                profile.email = issuer["email"]
                profile.phone = issuer["phone"]
                profile.street = issuer["street"]
                profile.city = issuer["city"]
                profile.zip = issuer["zip"]
                profile.country = issuer["country"]
                profile.ico = issuer["ico"]
                profile.dic = issuer["dic"]
                profile.bank_account = subject.bank_account

                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer={**issuer, "bank_account": ""},
                    issuer_source="form",
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit nastavení."),
                    status_code=500,
                    current_user=current_user,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                )

            return RedirectResponse(url="/settings?saved=1", status_code=303)

        @app.post("/settings/accounts/add")
        async def settings_account_add(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            sid = _current_subject_id()
            issuer, issuer_source = _load_issuer_for_current_subject(db)
            prefill = _account_prefill_from_form(
                form,
                default_country=str(issuer.get("country") or "CZ"),
                default_currency=str(issuer.get("default_currency") or "CZK"),
            )
            if len(prefill["currency"]) != 3:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Měna účtu musí mít 3 znaky.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                )
            if prefill["payment_sync_provider"] == "email_bank" and prefill["payment_sync_enabled"]:
                sender_filter = str(prefill["payment_sync_email_sender_filter"] or "").strip()
                if prefill["payment_sync_email_parser"] == "pending":
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error="Pro párování z bankovních e-mailů vyber konkrétní banku.",
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                    )
                if not sender_filter or not looks_like_email(sender_filter):
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error="Pro zvolený parser bankovního e-mailu chybí známý odesílatel.",
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                    )
            if prefill["payment_sync_provider"] == "fio_api" and prefill["payment_sync_enabled"]:
                fio_token_error = _test_fio_api_token(str(prefill["fio_api_token"] or "").strip())
                if fio_token_error:
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error=fio_token_error,
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                    )
            try:
                payload = resolve_bank_account(
                    account_number=prefill["account_number"],
                    iban=prefill["iban"],
                    bic=prefill["bic"],
                    country=prefill["country"],
                    label=prefill["label"],
                )
            except ValueError as exc:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=str(exc),
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                )

            try:
                subject = db.get(Subject, sid)
                if subject is None:
                    subject = Subject(id=sid)
                    db.add(subject)
                existing = _list_subject_bank_accounts(db, subject_id=sid)
                account = SubjectBankAccount(
                    subject_id=sid,
                    label=payload.label,
                    account_number=payload.number,
                    iban=payload.iban or None,
                    bic=payload.bic or None,
                    country=payload.country or "CZ",
                    currency=str(prefill["currency"] or "CZK"),
                    is_default=bool(prefill["is_default"] or not existing),
                    sort_order=len(existing) + 1,
                    payment_sync_provider=str(prefill["payment_sync_provider"] or "none"),
                    payment_sync_enabled=bool(prefill["payment_sync_enabled"]),
                    payment_sync_auto_pair=bool(prefill["payment_sync_auto_pair"]),
                    fio_api_token=(
                        _encode_fio_api_token(str(prefill["fio_api_token"] or "").strip() or None)
                        if prefill["payment_sync_provider"] == "fio_api"
                        else None
                    ),
                    payment_sync_alert_localpart=_generate_payment_sync_alert_localpart(db),
                    payment_sync_email_sender_filter=(str(prefill["payment_sync_email_sender_filter"] or "").strip().lower() or None),
                    payment_sync_email_subject_filter=(str(prefill["payment_sync_email_subject_filter"] or "").strip() or None),
                    payment_sync_email_parser=_normalize_payment_sync_email_parser(prefill.get("payment_sync_email_parser")),
                )
                _refresh_payment_sync_checkpoints(
                    account,
                    previous_provider="none",
                    previous_enabled=False,
                    previous_email_parser="pending",
                    previous_fio_token=None,
                    effective_fio_token=str(prefill["fio_api_token"] or "").strip() or None,
                )
                db.add(account)
                db.flush()
                if account.is_default:
                    _set_default_subject_bank_account(db, subject_id=sid, account_id=int(account.id))
                else:
                    _sync_subject_legacy_bank_account(subject, _list_subject_bank_accounts(db, subject_id=sid))
                    db.add(subject)
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se přidat účet."),
                    status_code=500,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                )
            return RedirectResponse(url=f"/settings?saved=1&edit_account={int(account.id)}#account-{int(account.id)}", status_code=303)

        @app.post("/settings/accounts/{account_id}/edit")
        async def settings_account_edit(account_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            sid = _current_subject_id()
            issuer, issuer_source = _load_issuer_for_current_subject(db)
            subject = _load_subject_for_current_session(db)
            account = db.scalar(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.id == int(account_id))
                .where(SubjectBankAccount.subject_id == int(sid))
            )
            if account is None:
                return JSONResponse(status_code=404, content={"detail": "Account not found"})

            prefill = _account_prefill_from_form(
                form,
                default_country=str(getattr(subject, "country", None) or issuer.get("country") or "CZ"),
                default_currency=str(
                    getattr(account, "currency", None)
                    or getattr(subject, "default_currency", None)
                    or issuer.get("default_currency")
                    or "CZK"
                ),
                account_id=int(account_id),
                has_existing_fio_token=bool(str(getattr(account, "fio_api_token", "") or "").strip()),
            )
            if len(prefill["currency"]) != 3:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Měna účtu musí mít 3 znaky.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=subject,
                    ),
                )
            if prefill["payment_sync_provider"] == "email_bank" and prefill["payment_sync_enabled"]:
                sender_filter = str(prefill["payment_sync_email_sender_filter"] or "").strip()
                if prefill["payment_sync_email_parser"] == "pending":
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error="Pro párování z bankovních e-mailů vyber konkrétní banku.",
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                        **_settings_subject_admin_context(
                            db,
                            request=request,
                            issuer=issuer,
                            subject=subject,
                        ),
                    )
                if not sender_filter or not looks_like_email(sender_filter):
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error="Pro zvolený parser bankovního e-mailu chybí známý odesílatel.",
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                        **_settings_subject_admin_context(
                            db,
                            request=request,
                            issuer=issuer,
                            subject=subject,
                        ),
                    )
            effective_fio_token = (
                str(prefill["fio_api_token"] or "").strip()
                or _decode_fio_api_token(getattr(account, "fio_api_token", None))
            )
            if prefill["payment_sync_provider"] == "fio_api" and prefill["payment_sync_enabled"]:
                fio_token_error = _test_fio_api_token(effective_fio_token)
                if fio_token_error:
                    return _render_settings_page(
                        request,
                        issuer=issuer,
                        issuer_source=issuer_source,
                        error=fio_token_error,
                        status_code=400,
                        bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                        account_prefill=prefill,
                        **_settings_subject_admin_context(
                            db,
                            request=request,
                            issuer=issuer,
                            subject=subject,
                        ),
                    )
            if str(prefill["fio_api_token"] or "").strip():
                prefill["has_fio_api_token"] = True
            try:
                payload = resolve_bank_account(
                    account_number=prefill["account_number"],
                    iban=prefill["iban"],
                    bic=prefill["bic"],
                    country=prefill["country"],
                    label=prefill["label"],
                )
            except ValueError as exc:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=str(exc),
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=subject,
                    ),
                )

            try:
                was_default = bool(account.is_default)
                previous_provider = str(getattr(account, "payment_sync_provider", "none") or "none")
                previous_enabled = bool(getattr(account, "payment_sync_enabled", False))
                previous_email_parser = str(getattr(account, "payment_sync_email_parser", "pending") or "pending")
                previous_fio_token = _decode_fio_api_token(getattr(account, "fio_api_token", None))
                account.label = payload.label
                account.account_number = payload.number
                account.iban = payload.iban or None
                account.bic = payload.bic or None
                account.country = payload.country or "CZ"
                account.currency = str(prefill["currency"] or "CZK")
                account.payment_sync_provider = str(prefill["payment_sync_provider"] or "none")
                account.payment_sync_enabled = bool(prefill["payment_sync_enabled"])
                account.payment_sync_auto_pair = bool(prefill["payment_sync_auto_pair"])
                if not str(getattr(account, "payment_sync_alert_localpart", "") or "").strip():
                    account.payment_sync_alert_localpart = _generate_payment_sync_alert_localpart(db)
                if prefill["payment_sync_provider"] != "fio_api":
                    account.fio_api_token = None
                elif str(prefill["fio_api_token"] or "").strip():
                    account.fio_api_token = _encode_fio_api_token(str(prefill["fio_api_token"]).strip())
                account.payment_sync_email_sender_filter = (
                    str(prefill["payment_sync_email_sender_filter"] or "").strip().lower() or None
                )
                account.payment_sync_email_subject_filter = (
                    str(prefill["payment_sync_email_subject_filter"] or "").strip() or None
                )
                account.payment_sync_email_parser = _normalize_payment_sync_email_parser(prefill.get("payment_sync_email_parser"))
                _refresh_payment_sync_checkpoints(
                    account,
                    previous_provider=previous_provider,
                    previous_enabled=previous_enabled,
                    previous_email_parser=previous_email_parser,
                    previous_fio_token=previous_fio_token,
                    effective_fio_token=effective_fio_token,
                )
                db.add(account)
                db.flush()

                if bool(prefill["is_default"]):
                    _set_default_subject_bank_account(db, subject_id=sid, account_id=int(account.id))
                elif was_default:
                    remaining = [row for row in _list_subject_bank_accounts(db, subject_id=sid) if int(row.id) != int(account.id)]
                    if remaining:
                        _set_default_subject_bank_account(db, subject_id=sid, account_id=int(remaining[0].id))
                    else:
                        account.is_default = True
                        account.sort_order = 0
                        db.add(account)

                refreshed_subject = db.get(Subject, sid)
                if refreshed_subject is not None:
                    _sync_subject_legacy_bank_account(refreshed_subject, _list_subject_bank_accounts(db, subject_id=sid))
                    db.add(refreshed_subject)
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit účet."),
                    status_code=500,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=sid),
                    account_prefill=prefill,
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=subject,
                    ),
                )
            return RedirectResponse(url=f"/settings?saved=1&edit_account={int(account.id)}#account-{int(account.id)}", status_code=303)

        @app.post("/settings/accounts/{account_id}/sync")
        async def settings_account_sync(account_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            account = db.scalar(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.id == int(account_id))
                .where(SubjectBankAccount.subject_id == int(sid))
            )
            if account is None:
                return JSONResponse(status_code=404, content={"detail": "Account not found"})

            try:
                result = _process_bank_sync(
                    db,
                    request=request,
                    subject_id=int(sid),
                    account_id=int(account_id),
                )
            except SQLAlchemyError as exc:
                db.rollback()
                return RedirectResponse(
                    url=f"/settings?error={quote(_safe_operation_error(exc, fallback='Nepodařilo se spustit párování.'), safe='')}#account-{account_id}",
                    status_code=303,
                )

            errors = list(result.get("errors") or [])
            if errors:
                return RedirectResponse(
                    url=f"/settings?error={quote(str(errors[0]), safe='')}#account-{account_id}",
                    status_code=303,
                )
            return RedirectResponse(
                url=f"/settings?info={quote(_bank_sync_notice(result), safe='')}#account-{account_id}",
                status_code=303,
            )

        @app.post("/settings/subjects/create")
        async def settings_subject_create(request: Request, db: Session = Depends(get_db)):
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return RedirectResponse(url=f"/login?next={quote('/settings#subjects-admin', safe='')}", status_code=303)

            form = await request.form()
            return_target = _safe_next_url(form.get("next"), "/settings#subjects-admin")
            issuer, issuer_source = _load_issuer_for_current_subject(db)
            current_subject = _load_subject_for_current_session(db)
            prefill = {
                "name": (form.get("name") or "").strip(),
                "email": (form.get("email") or "").strip(),
                "phone": (form.get("phone") or "").strip(),
                "street": (form.get("street") or "").strip(),
                "city": (form.get("city") or "").strip(),
                "zip": (form.get("zip") or "").strip(),
                "country": ((form.get("country") or getattr(current_subject, "country", None) or issuer.get("country") or "CZ").strip().upper() or "CZ"),
                "ico": (form.get("ico") or "").strip(),
                "dic": (form.get("dic") or "").strip(),
                "is_vat_payer": bool(form.get("is_vat_payer")),
                "is_vat_identified_person": bool(form.get("is_vat_identified_person")),
                "default_currency": ((form.get("default_currency") or getattr(current_subject, "default_currency", None) or issuer.get("default_currency") or "CZK").strip().upper() or "CZK"),
                "switch_after_create": bool(form.get("switch_after_create")),
            }

            if form.get("lookup_registry"):
                prefill, info, lookup_error = _lookup_subject_prefill_from_registry(db, prefill=prefill)
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    info=info,
                    error=lookup_error,
                    status_code=200 if lookup_error is None else 400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    subject_lookup_done=True,
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )

            if not prefill["name"]:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Nový subjekt musí mít alespoň název.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )
            if len(prefill["country"]) != 2:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Kód země subjektu musí mít 2 znaky.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )
            if not _is_supported_signup_subject_country(prefill["country"]):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Další subjekt může být zatím jen české IČO / český vystavovatel. Odběratelé ze zahraničí v kontaktech zůstávají povolení.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country="CZ", is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill={**prefill, "country": "CZ"},
                    ),
                )
            if len(prefill["default_currency"]) != 3:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Výchozí měna subjektu musí mít 3 znaky.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )
            if bool(prefill["is_vat_payer"]) and bool(prefill.get("is_vat_identified_person")):
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error="Nový subjekt nemůže být současně plátce DPH i identifikovaná osoba.",
                    status_code=400,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )

            try:
                subject = Subject(
                    name=prefill["name"],
                    email=prefill["email"],
                    phone=prefill["phone"],
                    street=prefill["street"],
                    city=prefill["city"],
                    zip=prefill["zip"],
                    country=prefill["country"],
                    ico=prefill["ico"],
                    dic=prefill["dic"],
                    is_vat_payer=bool(prefill["is_vat_payer"]),
                    is_vat_identified_person=bool(prefill.get("is_vat_identified_person")),
                    tax_regime="standard",
                    flat_tax_band="1",
                    flat_tax_income_profile="general",
                    default_currency=prefill["default_currency"],
                )
                db.add(subject)
                db.flush()
                ensure_subject_public_username(db, subject=subject)
                db.add(
                    UserSubject(
                        user_id=int(user_id),
                        subject_id=int(subject.id),
                        role="owner",
                        can_view=True,
                        can_edit=True,
                        can_issue=True,
                        can_export=True,
                    )
                )
                db.flush()
                db.commit()
                _refresh_current_session_access_context(
                    request,
                    db,
                    preferred_subject_id=int(subject.id) if bool(prefill["switch_after_create"]) else None,
                )
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vytvořit subjekt."),
                    status_code=500,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=prefill["country"], is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        subject_prefill=prefill,
                    ),
                )

            saved_target = _with_saved_flag(return_target, fallback="/settings#subjects-admin")
            if bool(prefill["switch_after_create"]):
                request.session["subject_id"] = int(subject.id)
                return RedirectResponse(
                    url=_with_query_params("/settings#subjects-admin", info=f"Subjekt {subject.name} jsem vytvořil a rovnou přepnul."),
                    status_code=303,
                )
            return RedirectResponse(url=saved_target, status_code=303)

        @app.post("/settings/subjects/{subject_id}/users/create")
        async def settings_subject_user_create(subject_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            return_target = _safe_next_url(form.get("next"), "/settings#subjects-admin")
            creator_user_id = _current_user_id_or_none(request)
            if creator_user_id is None:
                return RedirectResponse(url=f"/login?next={quote(return_target, safe='')}", status_code=303)
            if not _user_can_manage_subject_users(db, user_id=creator_user_id, subject_id=subject_id):
                raise HTTPException(status_code=403, detail="Access denied")

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            current_subject = _load_subject_for_current_session(db)
            allowed_roles = _subject_user_role_options(db, user_id=creator_user_id, subject_id=subject_id)
            temporary_password = str(form.get("password") or "")
            prefill = {
                "username": (form.get("username") or "").strip(),
                "email": (form.get("email") or "").strip(),
                "password": "",
                "role": ((form.get("role") or "manager").strip().lower() or "manager"),
                "can_view": bool(form.get("can_view")),
                "can_edit": bool(form.get("can_edit")),
                "can_issue": bool(form.get("can_issue")),
                "can_export": bool(form.get("can_export")),
            }
            def _render_subject_user_error(message: str, *, status_code: int) -> HTMLResponse:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=message,
                    status_code=status_code,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(country=getattr(current_subject, "country", None) or issuer.get("country") or "CZ", is_default=False),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        user_access_prefill=prefill,
                    ),
                )

            if not prefill["username"] or len(prefill["username"]) < 3:
                return _render_subject_user_error("Uživatelské jméno musí mít alespoň 3 znaky.", status_code=400)
            if not prefill["email"] or not looks_like_email(prefill["email"]):
                return _render_subject_user_error("Zadej platný e-mail nového účtu.", status_code=400)
            password_error = new_password_length_error(temporary_password)
            if password_error:
                return _render_subject_user_error(
                    password_error.replace("Heslo", "Dočasné heslo", 1),
                    status_code=400,
                )
            if str(prefill["role"] or "manager") not in allowed_roles:
                return _render_subject_user_error("Pro tenhle přístup nemáš oprávnění přiřadit vybranou roli.", status_code=403)

            (
                prefill["role"],
                prefill["can_view"],
                prefill["can_edit"],
                prefill["can_issue"],
                prefill["can_export"],
            ) = _normalize_subject_access_flags(
                role=str(prefill["role"] or "manager"),
                can_view=bool(prefill["can_view"]),
                can_edit=bool(prefill["can_edit"]),
                can_issue=bool(prefill["can_issue"]),
                can_export=bool(prefill["can_export"]),
            )

            try:
                existing = db.scalar(
                    select(User)
                    .where(or_(User.username == str(prefill["username"]), User.email == str(prefill["email"])))
                )
                if existing is not None:
                    raise ValueError("Uživatel se stejným jménem nebo e-mailem už existuje.")
                user = User(
                    username=str(prefill["username"]),
                    email=str(prefill["email"]),
                    password_hash=hash_password(temporary_password),
                    is_active=True,
                    email_verified_at=utc_now(),
                )
                db.add(user)
                db.flush()
                db.add(
                    UserSubject(
                        user_id=int(user.id),
                        subject_id=int(subject_id),
                        role=str(prefill["role"] or "manager"),
                        can_view=bool(prefill["can_view"]),
                        can_edit=bool(prefill["can_edit"]),
                        can_issue=bool(prefill["can_issue"]),
                        can_export=bool(prefill["can_export"]),
                    )
                )
                db.commit()
            except ValueError as exc:
                db.rollback()
                return _render_subject_user_error(str(exc), status_code=400)
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_subject_user_error(
                    _safe_operation_error(exc, fallback="Nepodařilo se vytvořit uživatelský účet."),
                    status_code=500,
                )

            _refresh_current_session_access_context(request, db)
            return RedirectResponse(
                url=_subject_access_post_save_target(request, db, next_url=return_target, subject_id=int(subject_id)),
                status_code=303,
            )

        @app.post("/settings/subjects/{subject_id}/users/link-existing")
        async def settings_subject_user_link_existing(subject_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            return_target = _safe_next_url(form.get("next"), "/settings#subjects-admin")
            actor_user_id = _current_user_id_or_none(request)
            if actor_user_id is None:
                return RedirectResponse(url=f"/login?next={quote(return_target, safe='')}", status_code=303)
            if int(subject_id) != _current_subject_id():
                raise HTTPException(status_code=403, detail="Access denied")
            if not _user_can_manage_subject_users(db, user_id=actor_user_id, subject_id=subject_id):
                raise HTTPException(status_code=403, detail="Access denied")

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            current_subject = _load_subject_for_current_session(db)
            prefill = {
                "identifier": (form.get("identifier") or "").strip(),
                "role": ((form.get("role") or "manager").strip().lower() or "manager"),
                "can_view": bool(form.get("can_view")),
                "can_edit": bool(form.get("can_edit")),
                "can_issue": bool(form.get("can_issue")),
                "can_export": bool(form.get("can_export")),
            }

            def _render_link_existing_error(message: str, *, status_code: int) -> HTMLResponse:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=message,
                    status_code=status_code,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(
                        country=getattr(current_subject, "country", None) or issuer.get("country") or "CZ",
                        is_default=False,
                    ),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        existing_user_link_prefill=prefill,
                    ),
                )

            if not prefill["identifier"]:
                return _render_link_existing_error("Zadej uživatelské jméno nebo e-mail existujícího účtu.", status_code=400)

            allowed_roles = _subject_user_role_options(db, user_id=actor_user_id, subject_id=subject_id)
            if str(prefill["role"] or "manager") not in allowed_roles:
                return _render_link_existing_error("Pro tenhle přístup nemáš oprávnění přiřadit vybranou roli.", status_code=403)

            (
                prefill["role"],
                prefill["can_view"],
                prefill["can_edit"],
                prefill["can_issue"],
                prefill["can_export"],
            ) = _normalize_subject_access_flags(
                role=str(prefill["role"] or "manager"),
                can_view=bool(prefill["can_view"]),
                can_edit=bool(prefill["can_edit"]),
                can_issue=bool(prefill["can_issue"]),
                can_export=bool(prefill["can_export"]),
            )

            existing_user = _find_user_by_identifier(db, identifier=str(prefill["identifier"]))
            if existing_user is None:
                return _render_link_existing_error("Žádný existující účet s tímhle jménem nebo e-mailem jsme nenašli.", status_code=404)
            if _user_subject_link(db, user_id=int(existing_user.id), subject_id=subject_id) is not None:
                return _render_link_existing_error("Tenhle účet už u zvoleného subjektu přístup má.", status_code=400)

            try:
                db.add(
                    UserSubject(
                        user_id=int(existing_user.id),
                        subject_id=int(subject_id),
                        role=str(prefill["role"] or "manager"),
                        can_view=bool(prefill["can_view"]),
                        can_edit=bool(prefill["can_edit"]),
                        can_issue=bool(prefill["can_issue"]),
                        can_export=bool(prefill["can_export"]),
                    )
                )
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_link_existing_error(
                    _safe_operation_error(exc, fallback="Nepodařilo se přidat existující účet k subjektu."),
                    status_code=500,
                )

            _refresh_current_session_access_context(request, db)
            return RedirectResponse(
                url=_subject_access_post_save_target(request, db, next_url=return_target, subject_id=int(subject_id)),
                status_code=303,
            )

        @app.post("/settings/subjects/{subject_id}/users/{link_id}/update")
        async def settings_subject_user_update(subject_id: int, link_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            return_target = _safe_next_url(form.get("next"), "/settings#subjects-admin")
            actor_user_id = _current_user_id_or_none(request)
            if actor_user_id is None:
                return RedirectResponse(url=f"/login?next={quote(return_target, safe='')}", status_code=303)
            if int(subject_id) != _current_subject_id():
                raise HTTPException(status_code=403, detail="Access denied")
            if not _user_can_manage_subject_users(db, user_id=actor_user_id, subject_id=subject_id):
                raise HTTPException(status_code=403, detail="Access denied")

            link = db.scalar(
                select(UserSubject)
                .where(UserSubject.id == int(link_id))
                .where(UserSubject.subject_id == int(subject_id))
            )
            if link is None:
                raise HTTPException(status_code=404, detail="Access link not found")

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            current_subject = _load_subject_for_current_session(db)
            prefill = {
                "role": ((form.get("role") or getattr(link, "role", "manager") or "manager").strip().lower() or "manager"),
                "can_view": bool(form.get("can_view")),
                "can_edit": bool(form.get("can_edit")),
                "can_issue": bool(form.get("can_issue")),
                "can_export": bool(form.get("can_export")),
            }

            def _render_update_error(message: str, *, status_code: int) -> HTMLResponse:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=message,
                    status_code=status_code,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(
                        country=getattr(current_subject, "country", None) or issuer.get("country") or "CZ",
                        is_default=False,
                    ),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                        access_edit_prefills={int(link_id): prefill},
                    ),
                )

            if not _user_can_manage_subject_link(db, user_id=actor_user_id, subject_id=subject_id, link=link):
                return _render_update_error("Tenhle přístup může upravovat jen owner subjektu.", status_code=403)

            allowed_roles = _subject_user_role_options(db, user_id=actor_user_id, subject_id=subject_id)
            if str(prefill["role"] or "manager") not in allowed_roles:
                return _render_update_error("Pro tenhle přístup nemáš oprávnění přiřadit vybranou roli.", status_code=403)

            (
                prefill["role"],
                prefill["can_view"],
                prefill["can_edit"],
                prefill["can_issue"],
                prefill["can_export"],
            ) = _normalize_subject_access_flags(
                role=str(prefill["role"] or "manager"),
                can_view=bool(prefill["can_view"]),
                can_edit=bool(prefill["can_edit"]),
                can_issue=bool(prefill["can_issue"]),
                can_export=bool(prefill["can_export"]),
            )

            current_role = _subject_role_value(link)
            if current_role == "owner" and str(prefill["role"] or "owner") != "owner" and _subject_owner_count(db, subject_id=subject_id) <= 1:
                return _render_update_error("Posledního ownera nejde degradovat na nižší roli.", status_code=400)

            preferred_subject_id: int | None = None
            if int(link.user_id) == int(actor_user_id) and int(subject_id) == _current_subject_id() and not bool(prefill["can_view"]):
                preferred_subject_id = _first_viewable_subject_id(
                    db,
                    user_id=actor_user_id,
                    exclude_subject_id=int(subject_id),
                )
                if preferred_subject_id is None:
                    return _render_update_error(
                        "U aktuálního subjektu si nemůžeš vypnout náhled, dokud nemáš k dispozici jiný přístup.",
                        status_code=400,
                    )

            try:
                link.role = str(prefill["role"] or current_role or "user")
                link.can_view = bool(prefill["can_view"])
                link.can_edit = bool(prefill["can_edit"])
                link.can_issue = bool(prefill["can_issue"])
                link.can_export = bool(prefill["can_export"])
                db.add(link)
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_update_error(
                    _safe_operation_error(exc, fallback="Nepodařilo se uložit změny přístupu."),
                    status_code=500,
                )

            _refresh_current_session_access_context(request, db, preferred_subject_id=preferred_subject_id)
            return RedirectResponse(
                url=_subject_access_post_save_target(request, db, next_url=return_target, subject_id=int(subject_id)),
                status_code=303,
            )

        @app.post("/settings/subjects/{subject_id}/users/{link_id}/delete")
        async def settings_subject_user_delete(subject_id: int, link_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            return_target = _safe_next_url(form.get("next"), "/settings#subjects-admin")
            actor_user_id = _current_user_id_or_none(request)
            if actor_user_id is None:
                return RedirectResponse(url=f"/login?next={quote(return_target, safe='')}", status_code=303)
            if int(subject_id) != _current_subject_id():
                raise HTTPException(status_code=403, detail="Access denied")
            if not _user_can_manage_subject_users(db, user_id=actor_user_id, subject_id=subject_id):
                raise HTTPException(status_code=403, detail="Access denied")

            link = db.scalar(
                select(UserSubject)
                .where(UserSubject.id == int(link_id))
                .where(UserSubject.subject_id == int(subject_id))
            )
            if link is None:
                raise HTTPException(status_code=404, detail="Access link not found")

            issuer, issuer_source = _load_issuer_for_current_subject(db)
            current_subject = _load_subject_for_current_session(db)

            def _render_delete_error(message: str, *, status_code: int) -> HTMLResponse:
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=message,
                    status_code=status_code,
                    bank_accounts=_bank_accounts_view_rows(db, subject_id=_current_subject_id()),
                    account_prefill=_blank_account_prefill(
                        country=getattr(current_subject, "country", None) or issuer.get("country") or "CZ",
                        is_default=False,
                    ),
                    **_settings_subject_admin_context(
                        db,
                        request=request,
                        issuer=issuer,
                        subject=current_subject,
                    ),
                )

            if not _user_can_manage_subject_link(db, user_id=actor_user_id, subject_id=subject_id, link=link):
                return _render_delete_error("Tenhle přístup může mazat jen owner subjektu.", status_code=403)
            if _subject_role_value(link) == "owner" and _subject_owner_count(db, subject_id=subject_id) <= 1:
                return _render_delete_error("Posledního ownera nejde odebrat ze subjektu.", status_code=400)

            preferred_subject_id: int | None = None
            if int(link.user_id) == int(actor_user_id) and int(subject_id) == _current_subject_id():
                preferred_subject_id = _first_viewable_subject_id(
                    db,
                    user_id=actor_user_id,
                    exclude_subject_id=int(subject_id),
                )
                if preferred_subject_id is None:
                    return _render_delete_error("Nemůžeš si odebrat poslední dostupný subjekt.", status_code=400)

            try:
                db.delete(link)
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_delete_error(
                    _safe_operation_error(exc, fallback="Nepodařilo se odebrat přístup."),
                    status_code=500,
                )

            _refresh_current_session_access_context(request, db, preferred_subject_id=preferred_subject_id)
            return RedirectResponse(
                url=_subject_access_post_save_target(request, db, next_url=return_target, subject_id=int(subject_id)),
                status_code=303,
            )




        @app.post("/settings/accounts/{account_id}/default")
        async def settings_account_default(account_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            account = db.scalar(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.id == int(account_id))
                .where(SubjectBankAccount.subject_id == int(sid))
            )
            if account is None:
                return JSONResponse(status_code=404, content={"detail": "Account not found"})
            try:
                _set_default_subject_bank_account(db, subject_id=sid, account_id=int(account_id))
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                return _render_settings_page(
                    request,
                    issuer=_load_issuer_for_current_subject(db)[0],
                    issuer_source=_load_issuer_for_current_subject(db)[1],
                    error=_safe_operation_error(exc, fallback="Nepodařilo se nastavit výchozí účet."),
                    status_code=500,
                    bank_accounts=[],
                )
            return RedirectResponse(url="/settings?saved=1", status_code=303)

        @app.post("/settings/accounts/{account_id}/delete")
        async def settings_account_delete(account_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            account = db.scalar(
                select(SubjectBankAccount)
                .where(SubjectBankAccount.id == int(account_id))
                .where(SubjectBankAccount.subject_id == int(sid))
            )
            if account is None:
                return JSONResponse(status_code=404, content={"detail": "Account not found"})
            try:
                invoices = db.scalars(select(Invoice).where(Invoice.bank_account_id == int(account.id))).all()
                for inv in invoices:
                    inv.bank_account_id = None
                    db.add(inv)
                was_default = bool(account.is_default)
                db.delete(account)
                db.flush()
                remaining = _list_subject_bank_accounts(db, subject_id=sid, ensure_bootstrap=False)
                if was_default and remaining:
                    _set_default_subject_bank_account(db, subject_id=sid, account_id=int(remaining[0].id))
                else:
                    subject = db.get(Subject, sid)
                    if subject is not None:
                        _sync_subject_legacy_bank_account(subject, remaining)
                        db.add(subject)
                db.commit()
            except SQLAlchemyError as exc:
                db.rollback()
                issuer, issuer_source = _load_issuer_for_current_subject(db)
                return _render_settings_page(
                    request,
                    issuer=issuer,
                    issuer_source=issuer_source,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se smazat účet."),
                    status_code=500,
                    bank_accounts=[
                        {
                            "id": int(acc.id),
                            "label": str(acc.label or ""),
                            "display": str(acc.account_number or "") or format_iban_for_display(acc.iban),
                            "iban": format_iban_for_display(acc.iban) if getattr(acc, "iban", None) else "",
                            "bic": str(acc.bic or ""),
                            "country": str(acc.country or ""),
                            "is_default": bool(acc.is_default),
                            "invoice_count": int(db.scalar(select(func.count(Invoice.id)).where(Invoice.bank_account_id == int(acc.id))) or 0),
                        }
                        for acc in _list_subject_bank_accounts(db, subject_id=sid)
                    ],
                )
            return RedirectResponse(url="/settings?saved=1", status_code=303)
    else:
        @app.get("/settings", response_class=HTMLResponse)
        def settings_page(request: Request):
            return _render_settings_page(
                request,
                issuer=_issuer_from_env(),
                issuer_source="env",
            )

    if _db_enabled:
        @app.get("/payments", response_class=HTMLResponse)
        def payments_page(
            request: Request,
            paid_page: int = 1,
            unmatched_page: int = 1,
            db: Session = Depends(get_db),
        ):
            sid = _current_subject_id()
            try:
                _refresh_current_session_access_context(request, db)
                try:
                    sid = int(request.session.get("subject_id") or sid)
                except Exception:
                    sid = _current_subject_id()
                accounts = db.scalars(
                    select(SubjectBankAccount)
                    .where(SubjectBankAccount.subject_id == int(sid))
                    .order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
                ).all()
                unmatched_base = (
                    select(BankTransaction)
                    .join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id)
                    .where(SubjectBankAccount.subject_id == int(sid))
                    .where(BankTransaction.direction == "incoming")
                    .where(BankTransaction.amount_cents > 0)
                    .where(BankTransaction.matched_invoice_id.is_(None))
                    .where(BankTransaction.payment_id.is_(None))
                )
                unmatched_total = int(
                    db.scalar(
                        select(func.count(BankTransaction.id))
                        .join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id)
                        .where(SubjectBankAccount.subject_id == int(sid))
                        .where(BankTransaction.direction == "incoming")
                        .where(BankTransaction.amount_cents > 0)
                        .where(BankTransaction.matched_invoice_id.is_(None))
                        .where(BankTransaction.payment_id.is_(None))
                    )
                    or 0
                )
                unmatched_pagination = _build_pagination_payload(
                    request,
                    page=_normalize_page_number(unmatched_page),
                    per_page=25,
                    total_count=unmatched_total,
                    page_param="unmatched_page",
                )
                unmatched_transactions = db.scalars(
                    unmatched_base
                    .order_by(BankTransaction.booked_on.desc(), BankTransaction.id.desc())
                    .offset(int(unmatched_pagination["offset"]))
                    .limit(int(unmatched_pagination["limit"]))
                ).all()
                paid_pagination = _build_pagination_payload(
                    request,
                    page=_normalize_page_number(paid_page),
                    per_page=25,
                    total_count=int(
                        db.scalar(
                            select(func.count(Payment.id))
                            .join(Invoice, Invoice.id == Payment.invoice_id)
                            .where(Invoice.subject_id == int(sid))
                        )
                        or 0
                    ),
                    page_param="paid_page",
                )
                paid_payments = db.scalars(
                    select(Payment)
                    .join(Invoice, Invoice.id == Payment.invoice_id)
                    .where(Invoice.subject_id == int(sid))
                    .options(selectinload(Payment.invoice).selectinload(Invoice.contact))
                    .order_by(Payment.paid_on.desc(), Payment.id.desc())
                    .offset(int(paid_pagination["offset"]))
                    .limit(int(paid_pagination["limit"]))
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Platby", db_error=str(exc))

            account_map = {int(account.id): account for account in accounts}
            account_rows: list[dict[str, object]] = []
            for account in accounts:
                provider = _normalize_payment_sync_provider(getattr(account, "payment_sync_provider", None))
                enabled = bool(getattr(account, "payment_sync_enabled", False)) and provider != "none"
                last_success = getattr(account, "payment_sync_last_success_at", None)
                last_error = str(getattr(account, "payment_sync_last_error", "") or "").strip()
                if not enabled:
                    health = "Vypnuto"
                    health_class = "muted"
                elif last_error:
                    health = "Chyba"
                    health_class = "danger"
                elif last_success is not None:
                    health = "Aktivní"
                    health_class = "success"
                else:
                    health = "Čeká na první kontrolu"
                    health_class = "warning"
                account_rows.append(
                    {
                        "id": int(account.id),
                        "label": str(account.label or "") or "Bankovní účet",
                        "display": str(account.account_number or "") or format_iban_for_display(account.iban),
                        "currency": str(account.currency or "CZK"),
                        "provider": dict(PAYMENT_SYNC_PROVIDER_OPTIONS).get(provider, provider),
                        "enabled": enabled,
                        "health": health,
                        "health_class": health_class,
                        "last_checked": getattr(account, "payment_sync_last_checked_at", None),
                        "last_success": last_success,
                        "last_error": last_error,
                    }
                )

            transaction_rows: list[dict[str, object]] = []
            for row in unmatched_transactions:
                account = account_map.get(int(row.subject_bank_account_id))
                candidates = _bank_sync_candidate_invoices(
                    db,
                    subject_id=int(sid),
                    account_id=int(row.subject_bank_account_id),
                    booked_on=row.booked_on,
                    amount_cents=int(row.amount_cents or 0),
                    currency=str(row.currency or "CZK"),
                    variable_symbol=getattr(row, "variable_symbol", None),
                    message=getattr(row, "message", None),
                )
                transaction_rows.append(
                    {
                        "id": int(row.id),
                        "booked_on": row.booked_on,
                        "amount": format_cents(int(row.amount_cents or 0), str(row.currency or "CZK")),
                        "variable_symbol": str(row.variable_symbol or ""),
                        "message": str(row.message or ""),
                        "counterparty": str(row.counterparty_name or row.counterparty_account or ""),
                        "account_label": str(getattr(account, "label", "") or getattr(account, "account_number", "") or ""),
                        "candidates": [
                            {
                                "id": int(invoice.id),
                                "number": str(invoice.number or ""),
                                "contact": str(getattr(getattr(invoice, "contact", None), "name", "") or ""),
                                "total": format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK")),
                                "status": str(getattr(invoice, "status", "") or ""),
                            }
                            for invoice in candidates[:3]
                        ],
                    }
                )

            paid_payment_rows: list[dict[str, object]] = []
            for payment in paid_payments:
                invoice = payment.invoice
                contact = getattr(invoice, "contact", None)
                paid_payment_rows.append(
                    {
                        "id": int(payment.id),
                        "paid_on": payment.paid_on,
                        "amount": format_cents(int(payment.amount_cents or 0), str(getattr(invoice, "currency", "") or "CZK")),
                        "note": str(payment.note or ""),
                        "invoice_id": int(getattr(invoice, "id", 0) or 0),
                        "invoice_number": str(getattr(invoice, "number", "") or ""),
                        "invoice_status": str(getattr(invoice, "status", "") or ""),
                        "contact": str(getattr(contact, "name", "") or ""),
                    }
                )

            return templates.TemplateResponse(
                request,
                "payments/index.html",
                {
                    "title": "Platby",
                    "account_rows": account_rows,
                    "transaction_rows": transaction_rows,
                    "unmatched_pagination": unmatched_pagination,
                    "paid_payment_rows": paid_payment_rows,
                    "paid_pagination": paid_pagination,
                },
            )

        @app.post("/payments/transactions/{transaction_id}/match")
        async def payments_match_transaction(transaction_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), "/payments#unmatched-payments")
            try:
                invoice_id = int(str(form.get("invoice_id") or "").strip())
            except Exception:
                return RedirectResponse(
                    url=_with_query_params(next_url, error="Vyber fakturu pro spárování platby."),
                    status_code=303,
                )

            sid = _current_subject_id()
            try:
                row = db.scalar(
                    select(BankTransaction)
                    .join(SubjectBankAccount, SubjectBankAccount.id == BankTransaction.subject_bank_account_id)
                    .where(BankTransaction.id == int(transaction_id))
                    .where(SubjectBankAccount.subject_id == int(sid))
                    .limit(1)
                )
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .limit(1)
                )
                if row is None or invoice is None:
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Platbu nebo fakturu jsem nenašel."),
                        status_code=303,
                    )
                if row.matched_invoice_id is not None or row.payment_id is not None:
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Tahleta platba už je spárovaná."),
                        status_code=303,
                    )
                if str(getattr(row, "direction", "") or "").strip().lower() != "incoming" or int(getattr(row, "amount_cents", 0) or 0) <= 0:
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Spárovat jde jen příchozí kladná platba."),
                        status_code=303,
                    )
                if int(getattr(invoice, "total_cents", 0) or 0) != int(getattr(row, "amount_cents", 0) or 0) or str(getattr(invoice, "currency", "") or "CZK").upper() != str(getattr(row, "currency", "") or "CZK").upper():
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Částka nebo měna platby nesedí s fakturou."),
                        status_code=303,
                    )

                account = db.get(SubjectBankAccount, int(row.subject_bank_account_id))
                if account is None or int(account.subject_id) != int(sid):
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Bankovní účet k platbě nepatří aktuálnímu subjektu."),
                        status_code=303,
                    )

                candidates = _bank_sync_candidate_invoices(
                    db,
                    subject_id=int(sid),
                    account_id=int(row.subject_bank_account_id),
                    booked_on=row.booked_on,
                    amount_cents=int(row.amount_cents or 0),
                    currency=str(row.currency or "CZK"),
                    variable_symbol=getattr(row, "variable_symbol", None),
                    message=getattr(row, "message", None),
                )
                if int(invoice.id) not in {int(candidate.id) for candidate in candidates}:
                    return RedirectResponse(
                        url=_with_query_params(next_url, error="Faktura není mezi bezpečnými kandidáty pro tuhle platbu."),
                        status_code=303,
                    )

                transaction = ImportedBankTransaction(
                    provider=str(row.provider or "fio_api"),
                    external_id=str(row.external_id or ""),
                    booked_on=row.booked_on,
                    amount_cents=int(row.amount_cents or 0),
                    currency=str(row.currency or "CZK"),
                    direction=str(row.direction or "incoming"),
                    variable_symbol=row.variable_symbol,
                    constant_symbol=row.constant_symbol,
                    specific_symbol=row.specific_symbol,
                    counterparty_account=row.counterparty_account,
                    counterparty_name=row.counterparty_name,
                    message=row.message,
                    raw_payload={},
                )
                payment = _apply_bank_transaction_match(
                    db,
                    invoice=invoice,
                    transaction=transaction,
                    request=request,
                )
                row.matched_invoice_id = int(invoice.id)
                row.payment_id = int(payment.id)
                row.matched_at = utc_now()
                db.add(row)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return RedirectResponse(
                    url=_with_query_params(
                        next_url,
                        error=_safe_operation_error(exc, fallback="Platbu se nepodařilo spárovat."),
                    ),
                    status_code=303,
                )
            except BankSyncError as exc:
                db.rollback()
                return RedirectResponse(
                    url=_with_query_params(
                        next_url,
                        error=_safe_operation_error(exc, fallback="Platbu se nepodařilo spárovat."),
                    ),
                    status_code=303,
                )

            _regenerate_invoice_pdf_best_effort(request, db, invoice_id=int(invoice.id), subject_id=int(sid))
            return RedirectResponse(
                url=_with_query_params(next_url, notice=f"Platba byla spárovaná s fakturou {invoice.number}."),
                status_code=303,
            )

        @app.get("/stats", response_class=HTMLResponse)
        def stats_page(request: Request, db: Session = Depends(get_db)):
            try:
                selected_year_raw = request.query_params.get("year")
                try:
                    selected_year = int(str(selected_year_raw or "").strip()) if selected_year_raw else None
                except ValueError:
                    selected_year = None
                subject = _load_subject_for_current_session(db)
                vat_view = str(request.query_params.get("vat_view") or "gross").strip().lower()
                stats_context = _build_invoice_stats_context(
                    db,
                    subject=subject,
                    selected_year=selected_year,
                    vat_view=vat_view,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return templates.TemplateResponse(
                    request,
                    "stats.html",
                    {
                        "db_enabled": False,
                        "db_error": _safe_db_error_message(exc),
                    },
                )

            return templates.TemplateResponse(
                request,
                "stats.html",
                {
                    "db_enabled": True,
                    **stats_context,
                },
            )
    else:
        @app.get("/stats", response_class=HTMLResponse)
        def stats_page(request: Request):
            return templates.TemplateResponse(
                request,
                "stats.html",
                {
                    "db_enabled": False,
                    "db_error": _safe_db_error_message(),
                },
            )

    # ------------------------------------------------------------------
    # Export dat (phase-35)
    # ------------------------------------------------------------------

    INVOICE_EXPORT_FORMAT_OPTIONS: list[tuple[str, str]] = [
        ("csv", "CSV přehled faktur"),
        ("csv_bundle", "CSV + položky v ZIPu"),
        ("xml", "Fakturek XML"),
        ("isdoc_zip", "ISDOC (ZIP)"),
        ("pohoda_xml", "POHODA XML"),
        ("money_s3_xml", "Money S3 XML"),
        ("pdf_single", "Jeden sloučený PDF"),
        ("pdf_zip", "Jednotlivé PDF v ZIPu"),
    ]
    INVOICE_EXPORT_STATUS_OPTIONS: list[tuple[str, str]] = [
        ("", "Všechny stavy"),
        ("draft", "Koncepty"),
        ("issued", "Vystavené"),
        ("sent", "Odeslané"),
        ("paid", "Zaplacené"),
        ("cancelled", "Stornované"),
    ]

    def _default_invoice_export_prefill() -> dict[str, object]:
        return {
            "q": "",
            "date_from": "",
            "date_to": "",
            "status": "",
            "document_type": "",
            "contact_ids": [],
            "overdue": False,
            "format": "csv",
        }

    def _normalize_int_list(values: list[object] | tuple[object, ...] | None) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in list(values or []):
            try:
                value = int(str(raw or "").strip())
            except Exception:
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _parse_export_date(value: object | None, *, label: str) -> date | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{label} má špatný formát data.") from exc

    def _invoice_export_file_base(
        *,
        subject_slug: str,
        invoices: list[Invoice],
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> str:
        parts = [subject_slug, "invoices"]
        if date_from and date_to:
            parts.append(f"{date_from.isoformat()}_{date_to.isoformat()}")
        elif date_from:
            parts.append(f"from_{date_from.isoformat()}")
        elif date_to:
            parts.append(f"to_{date_to.isoformat()}")
        elif invoices:
            issue_dates = [item.issue_date for item in invoices if isinstance(getattr(item, "issue_date", None), date)]
            if issue_dates:
                parts.append(f"{min(issue_dates).isoformat()}_{max(issue_dates).isoformat()}")
        parts.append(utc_now().strftime("%Y%m%d-%H%M%S"))
        return "-".join(part for part in parts if part)

    def _invoice_export_fieldnames() -> list[str]:
        return [
            "id",
            "number",
            "document_type",
            "source_invoice_id",
            "source_invoice_number",
            "status",
            "issue_date",
            "taxable_supply_date",
            "due_date",
            "paid_on",
            "currency",
            "items_total",
            "items_total_cents",
            "discount",
            "discount_cents",
            "subtotal_after_discount",
            "subtotal_after_discount_cents",
            "total",
            "total_cents",
            "rounding_adjustment",
            "rounding_adjustment_cents",
            "contact_id",
            "contact_name",
            "contact_email",
            "contact_ico",
            "series_name",
            "bank_account_label",
            "bank_account_number",
            "bank_account_iban",
            "bank_account_bic",
            "bank_account_country",
            "notes",
            "internal_notes",
            "issued_at",
            "sent_at",
            "reminder_sent_at",
            "public_url_enabled",
            "pdf_generated_at",
            "created_at",
            "updated_at",
        ]

    def _invoice_item_export_fieldnames() -> list[str]:
        return [
            "invoice_id",
            "invoice_number",
            "invoice_status",
            "issue_date",
            "taxable_supply_date",
            "currency",
            "line_no",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "unit_price_cents",
            "vat_rate",
            "line_net",
            "line_net_cents",
            "line_vat",
            "line_vat_cents",
            "line_total",
            "line_total_cents",
            "created_at",
            "updated_at",
        ]

    def _build_invoice_export_xml_bytes(
        db: Session,
        *,
        invoices: list[Invoice],
        subject_id: int,
    ) -> bytes:
        invoice_ids = [int(invoice.id) for invoice in invoices]
        item_rows = _export_invoice_items_rows(db, subject_id=int(subject_id), invoice_ids=invoice_ids)
        items_by_invoice: dict[int, list[dict[str, object]]] = defaultdict(list)
        for row in item_rows:
            items_by_invoice[int(row.get("invoice_id", 0) or 0)].append(row)

        root = ET.Element(
            "fakturek_export",
            {
                "kind": "invoice_export",
                "subject_id": str(int(subject_id)),
                "generated_at_utc": utc_now().isoformat(timespec="seconds"),
            },
        )
        invoices_el = ET.SubElement(root, "invoices", {"count": str(len(invoices))})

        for invoice in invoices:
            invoice_el = ET.SubElement(
                invoices_el,
                "invoice",
                {
                    "id": str(int(invoice.id)),
                    "number": str(invoice.number or ""),
                    "document_type": _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice")),
                    "status": str(invoice.status or ""),
                },
            )
            contact = getattr(invoice, "contact", None)
            invoice_values = {
                "issue_date": _iso_to_export_str(getattr(invoice, "issue_date", None)),
                "due_date": _iso_to_export_str(getattr(invoice, "due_date", None)),
                "paid_on": _iso_to_export_str(getattr(invoice, "paid_on", None)),
                "currency": str(invoice.currency or ""),
                "total_cents": str(int(getattr(invoice, "total_cents", 0) or 0)),
                "discount_cents": str(int(getattr(invoice, "discount_cents", 0) or 0)),
                "rounding_adjustment_cents": str(int(getattr(invoice, "rounding_adjustment_cents", 0) or 0)),
                "notes": str(getattr(invoice, "notes", "") or ""),
                "internal_notes": str(getattr(invoice, "internal_notes", "") or ""),
                "source_invoice_number": _invoice_source_invoice_number(db, invoice=invoice),
            }
            for key, value in invoice_values.items():
                ET.SubElement(invoice_el, key).text = value

            contact_el = ET.SubElement(invoice_el, "contact")
            for key, value in {
                "id": str(int(getattr(contact, "id", 0) or 0)),
                "name": str(getattr(contact, "name", "") or ""),
                "email": str(getattr(contact, "email", "") or ""),
                "ico": str(getattr(contact, "ico", "") or ""),
            }.items():
                ET.SubElement(contact_el, key).text = value

            items_el = ET.SubElement(invoice_el, "items", {"count": str(len(items_by_invoice.get(int(invoice.id), [])))})
            for item_row in items_by_invoice.get(int(invoice.id), []):
                item_el = ET.SubElement(items_el, "item", {"line_no": str(int(item_row.get("line_no", 0) or 0))})
                for key in [
                    "description",
                    "quantity",
                    "unit",
                    "unit_price",
                    "unit_price_cents",
                    "vat_rate",
                    "line_net",
                    "line_net_cents",
                    "line_vat",
                    "line_vat_cents",
                    "line_total",
                    "line_total_cents",
                ]:
                    ET.SubElement(item_el, key).text = str(item_row.get(key, "") or "")

        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _invoice_export_items_by_invoice(
        db: Session,
        *,
        invoices: list[Invoice],
        subject_id: int,
    ) -> dict[int, list[dict[str, object]]]:
        invoice_ids = [int(invoice.id) for invoice in invoices]
        item_rows = _export_invoice_items_rows(db, subject_id=int(subject_id), invoice_ids=invoice_ids)
        items_by_invoice: dict[int, list[dict[str, object]]] = defaultdict(list)
        for row in item_rows:
            items_by_invoice[int(row.get("invoice_id", 0) or 0)].append(row)
        return items_by_invoice

    def _build_invoice_export_pohoda_xml_bytes(
        db: Session,
        *,
        invoices: list[Invoice],
        subject_id: int,
    ) -> bytes:
        return build_pohoda_invoice_export_bytes(
            invoices=invoices,
            items_by_invoice=_invoice_export_items_by_invoice(db, invoices=invoices, subject_id=subject_id),
            subject_id=int(subject_id),
        )

    def _build_invoice_export_money_s3_xml_bytes(
        db: Session,
        *,
        invoices: list[Invoice],
        subject_id: int,
    ) -> bytes:
        return build_money_s3_invoice_export_bytes(
            invoices=invoices,
            items_by_invoice=_invoice_export_items_by_invoice(db, invoices=invoices, subject_id=subject_id),
            subject_id=int(subject_id),
        )

    def _invoice_pdf_bytes_for_export(
        request: Request,
        db: Session,
        *,
        invoice: Invoice,
    ) -> bytes:
        if str(invoice.status or "").strip().lower() != "draft" and _invoice_cached_pdf_is_fresh(invoice):
            cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
            if cached is not None and bytes(cached).startswith(b"%PDF"):
                return bytes(cached)

        items = db.scalars(
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id == int(invoice.id))
            .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
        ).all()
        ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))

        try:
            html = templates.get_template("invoices/print.html").render(
                {
                    "request": request,
                    **ctx,
                    "pdf_mode": True,
                    "app_css": _load_app_css(),
                }
            )
            return render_html_pdf_bytes(html, base_url=project_root)
        except Exception:
            pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)
            return render_invoice_pdf_bytes(pdf_data)

    def _build_invoice_pdf_zip_bytes(
        request: Request,
        db: Session,
        *,
        invoices: list[Invoice],
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for invoice in invoices:
                pdf_bytes = _invoice_pdf_bytes_for_export(request, db, invoice=invoice)
                filename = safe_filename_base(str(invoice.number or f"invoice-{int(invoice.id)}"), fallback=f"invoice-{int(invoice.id)}")
                zf.writestr(f"{filename}.pdf", pdf_bytes)
        return buf.getvalue()

    def _build_invoice_isdoc_zip_bytes(
        db: Session,
        *,
        invoices: list[Invoice],
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for invoice in invoices:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
                ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))
                isdoc_bytes = build_isdoc_bytes(invoice=invoice, ctx=ctx)
                filename = safe_filename_base(str(invoice.number or f"invoice-{int(invoice.id)}"), fallback=f"invoice-{int(invoice.id)}")
                zf.writestr(f"{filename}.isdoc", isdoc_bytes)
            zf.writestr(
                "README.txt",
                "\n".join(["Fakturek - ISDOC export faktur", f"Pocet faktur: {len(invoices)}"]).encode("utf-8"),
            )
        return buf.getvalue()

    def _build_invoice_pdf_merged_bytes(
        request: Request,
        db: Session,
        *,
        invoices: list[Invoice],
    ) -> bytes:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for invoice in invoices:
            pdf_bytes = _invoice_pdf_bytes_for_export(request, db, invoice=invoice)
            writer.append(io.BytesIO(pdf_bytes))
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    if _db_enabled:
        @app.get("/exports/data.zip")
        def export_data_zip(request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                zip_bytes, filename = _build_full_export_zip_bytes(db, subject_id=int(sid))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export dat", db_error=str(exc), status_code=500)
            return Response(
                content=zip_bytes,
                media_type="application/zip",
                headers={"Content-Disposition": _attachment_disposition(filename)},
            )

        @app.post("/exports/invoices")
        async def export_invoices_custom(request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()

            prefill = _default_invoice_export_prefill()
            prefill["q"] = " ".join(str(form.get("q") or "").split()).strip()
            prefill["date_from"] = str(form.get("date_from") or "").strip()
            prefill["date_to"] = str(form.get("date_to") or "").strip()
            prefill["status"] = str(form.get("status") or "").strip().lower()
            prefill["document_type"] = str(form.get("document_type") or "").strip().lower()
            prefill["contact_ids"] = _normalize_int_list(form.getlist("contact_ids"))
            prefill["overdue"] = str(form.get("overdue") or "").strip().lower() in {"1", "true", "on", "yes"}
            prefill["format"] = str(form.get("format") or "csv").strip().lower() or "csv"

            def _render_export_error(error_message: str, *, status_code: int = 400):
                try:
                    runs = (
                        db.scalars(
                            select(ImportRun)
                            .where(ImportRun.subject_id == int(sid))
                            .order_by(ImportRun.id.desc())
                            .limit(50)
                        )
                        .all()
                    )
                    export_contacts = _load_export_contacts(db, subject_id=int(sid))
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title="Export/Import", db_error=str(exc), status_code=500)
                return templates.TemplateResponse(
                    request,
                    "imports/list.html",
                    {
                        "db_enabled": True,
                        "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                        "notice": None,
                        "error": error_message,
                        "runs": runs,
                        "prefill": {"source": "fakturoid"},
                        "export_prefill": prefill,
                        "export_contact_options": export_contacts,
                        "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                        "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                        "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                        "import_source_options": _import_source_options(),
                        "max_upload_mb": int(getattr(settings, "import_max_upload_mb", 25) or 25),
                        "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                    },
                    status_code=status_code,
                )

            if prefill["format"] not in {value for value, _label in INVOICE_EXPORT_FORMAT_OPTIONS}:
                return _render_export_error("Vyber platný formát exportu.")

            try:
                date_from = _parse_export_date(prefill.get("date_from"), label="Datum od")
                date_to = _parse_export_date(prefill.get("date_to"), label="Datum do")
            except ValueError as exc:
                return _render_export_error(str(exc))

            if date_from and date_to and date_from > date_to:
                return _render_export_error("Datum od musí být dřív nebo stejně jako datum do.")

            try:
                invoices = _load_export_invoices(
                    db,
                    subject_id=int(sid),
                    q=str(prefill.get("q") or ""),
                    status=str(prefill.get("status") or ""),
                    contact_ids=list(prefill.get("contact_ids") or []),
                    document_type=str(prefill.get("document_type") or ""),
                    overdue=bool(prefill.get("overdue")),
                    issue_date_from=date_from,
                    issue_date_to=date_to,
                )
                subject = _load_subject_for_current_session(db)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export faktur", db_error=str(exc), status_code=500)

            if not invoices:
                return _render_export_error("Pro zadané filtry jsem nenašel žádné faktury.")

            subject_slug = _export_subject_slug(subject, subject_id=int(sid))
            file_base = _invoice_export_file_base(
                subject_slug=subject_slug,
                invoices=invoices,
                date_from=date_from,
                date_to=date_to,
            )
            invoice_ids = [int(invoice.id) for invoice in invoices]

            if prefill["format"] == "csv":
                rows = _export_invoices_rows(
                    db,
                    subject_id=int(sid),
                    q=str(prefill.get("q") or ""),
                    status=str(prefill.get("status") or ""),
                    contact_ids=list(prefill.get("contact_ids") or []),
                    document_type=str(prefill.get("document_type") or ""),
                    overdue=bool(prefill.get("overdue")),
                    issue_date_from=date_from,
                    issue_date_to=date_to,
                )
                return _csv_attachment_response(
                    _invoice_export_fieldnames(),
                    rows,
                    filename=f"{file_base}.csv",
                )

            if prefill["format"] == "csv_bundle":
                invoice_rows = _export_invoices_rows(
                    db,
                    subject_id=int(sid),
                    q=str(prefill.get("q") or ""),
                    status=str(prefill.get("status") or ""),
                    contact_ids=list(prefill.get("contact_ids") or []),
                    document_type=str(prefill.get("document_type") or ""),
                    overdue=bool(prefill.get("overdue")),
                    issue_date_from=date_from,
                    issue_date_to=date_to,
                )
                item_rows = _export_invoice_items_rows(db, subject_id=int(sid), invoice_ids=invoice_ids)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("invoices.csv", _csv_bytes_from_rows(_invoice_export_fieldnames(), invoice_rows))
                    zf.writestr("invoice_items.csv", _csv_bytes_from_rows(_invoice_item_export_fieldnames(), item_rows))
                    zf.writestr(
                        "README.txt",
                        "\n".join(
                            [
                                "Fakturek – export faktur",
                                f"Počet faktur: {len(invoice_rows)}",
                                f"Počet položek: {len(item_rows)}",
                            ]
                        ).encode("utf-8"),
                    )
                return Response(
                    content=buf.getvalue(),
                    media_type="application/zip",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}.zip")},
                )

            if prefill["format"] == "xml":
                xml_bytes = _build_invoice_export_xml_bytes(
                    db,
                    invoices=invoices,
                    subject_id=int(sid),
                )
                return Response(
                    content=xml_bytes,
                    media_type="application/xml; charset=utf-8",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}.xml")},
                )

            if prefill["format"] == "isdoc_zip":
                try:
                    zip_bytes = _build_invoice_isdoc_zip_bytes(db, invoices=invoices)
                except Exception as exc:
                    return _render_export_error(
                        _safe_operation_error(exc, fallback="ZIP s ISDOC se nepodařilo vygenerovat."),
                        status_code=500,
                    )
                return Response(
                    content=zip_bytes,
                    media_type="application/zip",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}-isdoc.zip")},
                )

            if prefill["format"] == "pohoda_xml":
                xml_bytes = _build_invoice_export_pohoda_xml_bytes(
                    db,
                    invoices=invoices,
                    subject_id=int(sid),
                )
                return Response(
                    content=xml_bytes,
                    media_type="application/xml; charset=utf-8",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}-pohoda.xml")},
                )

            if prefill["format"] == "money_s3_xml":
                xml_bytes = _build_invoice_export_money_s3_xml_bytes(
                    db,
                    invoices=invoices,
                    subject_id=int(sid),
                )
                return Response(
                    content=xml_bytes,
                    media_type="application/xml; charset=utf-8",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}-money-s3.xml")},
                )

            if prefill["format"] == "pdf_single":
                try:
                    pdf_bytes = _build_invoice_pdf_merged_bytes(request, db, invoices=invoices)
                except Exception as exc:
                    return _render_export_error(
                        _safe_operation_error(exc, fallback="Sloučené PDF se nepodařilo vygenerovat."),
                        status_code=500,
                    )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}.pdf")},
                )

            if prefill["format"] == "pdf_zip":
                try:
                    zip_bytes = _build_invoice_pdf_zip_bytes(request, db, invoices=invoices)
                except Exception as exc:
                    return _render_export_error(
                        _safe_operation_error(exc, fallback="ZIP s PDF se nepodařilo vygenerovat."),
                        status_code=500,
                    )
                return Response(
                    content=zip_bytes,
                    media_type="application/zip",
                    headers={"Content-Disposition": _attachment_disposition(f"{file_base}-pdf.zip")},
                )

            return _render_export_error("Vybraný formát exportu zatím neumím zpracovat.")
    else:
        @app.get("/exports/data.zip")
        def export_data_zip_disabled(request: Request):
            return _render_db_disabled(request, title="Export dat")

        @app.post("/exports/invoices")
        async def export_invoices_custom_disabled(request: Request):
            return _render_db_disabled(request, title="Export faktur")

    # ------------------------------------------------------------------
    # Import (phase-24/25)
    # ------------------------------------------------------------------

    IMPORT_SOURCE_OPTIONS: list[dict[str, str]] = [
        {
            "value": "fakturoid",
            "label": "Fakturoid export",
            "description": "XML faktury, CSV kontakty nebo ZIP z Fakturoidu. Nejbezpečnější cesta pro kompletní migraci.",
            "accept": ".xml,.csv,.zip,.pdf,application/xml,text/csv,application/zip,application/pdf",
        },
        {
            "value": "pohoda_xml",
            "label": "POHODA XML",
            "description": "Skutečný import faktur z POHODA XML včetně partnera, položek, bankovního účtu a VS.",
            "accept": ".xml,.zip,application/xml,application/zip",
        },
        {
            "value": "money_s3_xml",
            "label": "Money S3 XML",
            "description": "Strukturovaný import vydaných faktur z Money S3 XML včetně partnera, položek a platebních údajů.",
            "accept": ".xml,.zip,application/xml,application/zip",
        },
        {
            "value": "contacts_csv",
            "label": "Kontakty CSV",
            "description": "Jednodušší import kontaktů z jiného systému. Hodí se pro CRM exporty nebo ručně upravené CSV.",
            "accept": ".csv,.zip,text/csv,application/zip",
        },
        {
            "value": "isdoc",
            "label": "ISDOC",
            "description": "Import ISDOC faktur ve formátu .isdoc, XML nebo ZIP s ISDOC soubory.",
            "accept": ".isdoc,.xml,.zip,application/xml,application/zip",
        },
        {
            "value": "invoice_xml",
            "label": "Faktury XML",
            "description": "Samotné faktury v XML, ISDOC nebo ZIP s XML. Dobré pro strukturovaný přesun bez kontaktového CSV.",
            "accept": ".xml,.isdoc,.zip,application/xml,application/zip",
        },
        {
            "value": "pdf_archive",
            "label": "PDF / ZIP archiv",
            "description": "Doplňkový import PDF faktur nebo ZIPu s PDF. Vhodné hlavně pro archiv a dohledání podkladů.",
            "accept": ".pdf,.zip,application/pdf,application/zip",
        },
    ]

    def _import_source_options() -> list[dict[str, str]]:
        return [dict(option) for option in IMPORT_SOURCE_OPTIONS]

    IMPORT_CONTACT_MAPPING_FIELDS: list[tuple[str, str]] = [
        ("external_id", "Externí ID"),
        ("name", "Název kontaktu"),
        ("email", "E-mail"),
        ("phone", "Telefon"),
        ("street", "Ulice"),
        ("city", "Město"),
        ("zip", "PSČ"),
        ("country", "Země"),
        ("ico", "IČO"),
        ("dic", "DIČ"),
        ("fixed_variable_symbol", "Pevný VS"),
    ]

    IMPORT_CONTACT_CONFLICT_OPTIONS: list[tuple[str, str]] = [
        ("merge_existing", "Sloučit s existujícím kontaktem a doplnit chybějící údaje"),
        ("skip_existing", "Existující kontakt jen znovu použít, ale nic na něm neupravovat"),
        ("create_new", "Vytvořit nový kontakt i při shodě podle IČO, e-mailu nebo názvu"),
    ]

    IMPORT_INVOICE_CONFLICT_OPTIONS: list[tuple[str, str]] = [
        ("skip", "Přeskočit fakturu, když číslo už existuje"),
        ("renumber", "Importovat ji i tak a přidělit jí další volné číslo"),
    ]

    def _import_source_label(value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        for option in IMPORT_SOURCE_OPTIONS:
            if str(option.get("value") or "").strip().lower() == normalized:
                return str(option.get("label") or value or "")
        return str(value or "")

    def _import_run_summary_payload(run) -> dict[str, object]:
        try:
            payload = json.loads(str(getattr(run, "summary_json", "") or ""))
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _import_run_config(run) -> dict[str, object]:
        payload = _import_run_summary_payload(run)
        config = payload.get("config")
        return dict(config) if isinstance(config, dict) else {}

    def _merge_import_run_summary_payload(
        run,
        *,
        config: dict[str, object] | None = None,
    ) -> str:
        payload = _import_run_summary_payload(run)
        if config is not None:
            payload["config"] = config
        return json.dumps(payload, ensure_ascii=False)

    if _db_enabled:
        @app.get("/imports", response_class=HTMLResponse)
        def imports_page(
            request: Request,
            db: Session = Depends(get_db),
            uploaded: bool = False,
            duplicate: bool = False,
            processed: bool = False,
        ):
            notice = None
            if uploaded:
                notice = "Soubor byl nahrán."
            if duplicate:
                notice = "Soubor už byl nahrán dříve – zobrazuji původní běh."
            if processed:
                notice = "Import byl zpracován."

            try:
                sid = _current_subject_id()
                runs = (
                    db.scalars(
                        select(ImportRun)
                        .where(ImportRun.subject_id == int(sid))
                        .order_by(ImportRun.id.desc())
                        .limit(50)
                    )
                    .all()
                )
                export_contacts = _load_export_contacts(db, subject_id=int(sid))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc))

            return templates.TemplateResponse(
                request,
                "imports/list.html",
                {
                    "db_enabled": True,
                    "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                    "notice": notice,
                    "error": None,
                    "runs": runs,
                    "prefill": {"source": "fakturoid"},
                    "export_prefill": _default_invoice_export_prefill(),
                    "export_contact_options": export_contacts,
                    "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                    "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                    "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                    "import_source_options": _import_source_options(),
                    "max_upload_mb": int(getattr(settings, "import_max_upload_mb", 25) or 25),
                    "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                },
            )

        @app.post("/imports")
        async def imports_upload(request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await _request_form_once(request)
            source = (form.get("source") or "fakturoid").strip().lower() or "fakturoid"
            upload = form.get("file")

            if upload is None or not getattr(upload, "filename", None):
                return templates.TemplateResponse(
                    request,
                    "imports/list.html",
                    {
                        "db_enabled": True,
                        "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                        "notice": None,
                        "error": "Chybí soubor.",
                        "runs": [],
                        "prefill": {"source": source},
                        "export_prefill": _default_invoice_export_prefill(),
                        "export_contact_options": [],
                        "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                        "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                        "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                        "import_source_options": _import_source_options(),
                        "max_upload_mb": int(getattr(settings, "import_max_upload_mb", 25) or 25),
                        "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                    },
                    status_code=400,
                )

            max_mb = int(getattr(settings, "import_max_upload_mb", 25) or 25)
            max_bytes = max(1, max_mb) * 1024 * 1024

            try:
                tmp_path, sha256_hex, size_bytes = await _save_upload_to_temp(upload, max_bytes=max_bytes)
            except ValueError as exc:
                return templates.TemplateResponse(
                    request,
                    "imports/list.html",
                    {
                        "db_enabled": True,
                        "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                        "notice": None,
                        "error": str(exc),
                        "runs": [],
                        "prefill": {"source": source},
                        "export_prefill": _default_invoice_export_prefill(),
                        "export_contact_options": [],
                        "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                        "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                        "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                        "import_source_options": _import_source_options(),
                        "max_upload_mb": max_mb,
                        "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                    },
                    status_code=400,
                )
            except Exception as exc:
                return templates.TemplateResponse(
                    request,
                    "imports/list.html",
                    {
                        "db_enabled": True,
                        "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                        "notice": None,
                        "error": _safe_operation_error(exc, fallback="Nepodařilo se přečíst soubor."),
                        "runs": [],
                        "prefill": {"source": source},
                        "export_prefill": _default_invoice_export_prefill(),
                        "export_contact_options": [],
                        "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                        "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                        "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                        "import_source_options": _import_source_options(),
                        "max_upload_mb": max_mb,
                        "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                    },
                    status_code=400,
                )

            # File-level idempotence: reuse existing run with the same SHA256 (same subject + source).
            try:
                existing = db.scalar(
                    select(ImportRun)
                    .where(ImportRun.subject_id == int(sid))
                    .where(ImportRun.source == str(source))
                    .where(ImportRun.file_sha256 == str(sha256_hex))
                    .order_by(ImportRun.id.desc())
                    .limit(1)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc), status_code=503)

            if existing is not None:
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                return RedirectResponse(url=f"/imports/{int(existing.id)}?duplicate=1", status_code=303)

            # Create a new run row, then move the temp file under IMPORT_STORAGE_DIR.
            run = ImportRun(
                subject_id=int(sid),
                source=str(source),
                status="uploaded",
                file_name=str(getattr(upload, "filename", "") or ""),
                file_sha256=str(sha256_hex),
                file_size_bytes=int(size_bytes),
                mime_type=str(getattr(upload, "content_type", "") or ""),
                summary_json=json.dumps(
                    {
                        "phase": 26,
                        "note": "uploaded (ready to process: CSV/XML/ZIP)",
                    }
                ),
            )
            db.add(run)

            try:
                db.flush()
                relpath = _import_file_relpath(
                    subject_id=int(sid),
                    run_id=int(run.id),
                    original_filename=str(getattr(upload, "filename", "") or ""),
                )
                full_path = _safe_resolve_under_root(import_storage_root, relpath)
                full_path.parent.mkdir(parents=True, exist_ok=True)

                _persist_uploaded_temp_file(tmp_path, full_path)

                run.file_path = relpath.as_posix()
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc), status_code=500)
            except Exception as exc:
                db.rollback()
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                return templates.TemplateResponse(
                    request,
                    "imports/list.html",
                    {
                        "db_enabled": True,
                        "can_export": _current_request_can_export_subject(db, request=request, subject_id=int(sid)),
                        "notice": None,
                        "error": _safe_operation_error(exc, fallback="Nepodařilo se uložit soubor."),
                        "runs": [],
                        "prefill": {"source": source},
                        "export_prefill": _default_invoice_export_prefill(),
                        "export_contact_options": [],
                        "export_format_options": INVOICE_EXPORT_FORMAT_OPTIONS,
                        "invoice_status_options": INVOICE_EXPORT_STATUS_OPTIONS,
                        "invoice_document_type_options": INVOICE_DOCUMENT_TYPE_OPTIONS,
                        "import_source_options": _import_source_options(),
                        "max_upload_mb": max_mb,
                        "import_storage_dir": str(getattr(settings, "import_storage_dir", "var/imports") or "var/imports"),
                    },
                    status_code=500,
                )

            return RedirectResponse(url=f"/imports/{int(run.id)}?uploaded=1", status_code=303)

        @app.post("/imports/{run_id}/config")
        async def imports_update_config(run_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                run = db.scalar(
                    select(ImportRun)
                    .where(ImportRun.id == int(run_id))
                    .where(ImportRun.subject_id == int(sid))
                    .limit(1)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc))

            if run is None:
                raise HTTPException(status_code=404, detail="Import run not found")

            await _verify_csrf(request)
            form = await _request_form_once(request)
            mapping: dict[str, str] = {}
            for field_name, _label in IMPORT_CONTACT_MAPPING_FIELDS:
                value = str(form.get(f"map_{field_name}") or "").strip()
                if value:
                    mapping[field_name] = value

            config = _import_run_config(run)
            config["contact_csv_mapping"] = mapping
            contact_conflict_mode = str(form.get("contact_conflict_mode") or "merge_existing").strip().lower()
            if contact_conflict_mode not in {"merge_existing", "skip_existing", "create_new"}:
                contact_conflict_mode = "merge_existing"
            invoice_number_conflict_mode = str(form.get("invoice_number_conflict_mode") or "skip").strip().lower()
            if invoice_number_conflict_mode not in {"skip", "renumber"}:
                invoice_number_conflict_mode = "skip"
            config["contact_conflict_mode"] = contact_conflict_mode
            config["invoice_number_conflict_mode"] = invoice_number_conflict_mode
            try:
                run.summary_json = _merge_import_run_summary_payload(run, config=config)
                db.add(run)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc), status_code=500)

            return RedirectResponse(url=f"/imports/{int(run.id)}", status_code=303)

        @app.post("/imports/{run_id}/process")
        async def imports_process(run_id: int, request: Request, db: Session = Depends(get_db)):
            """Parse + import the uploaded file for the given run."""

            sid = _current_subject_id()
            try:
                run = db.scalar(
                    select(ImportRun)
                    .where(ImportRun.id == int(run_id))
                    .where(ImportRun.subject_id == int(sid))
                    .limit(1)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc))

            if run is None:
                raise HTTPException(status_code=404, detail="Import run not found")

            # Mark as running.
            try:
                run.status = "running"
                run.finished_at = None
                db.add(run)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc))

            # Execute processing.
            try:
                from fakturek.fakturoid_import import process_import_run, summary_to_json

                summary = process_import_run(
                    db,
                    run=run,
                    subject_id=int(sid),
                    import_storage_root=import_storage_root,
                )
                run.status = "finished"
                run.finished_at = utc_now()
                run.summary_json = summary_to_json(summary)
                db.add(run)
                db.commit()
                return RedirectResponse(url=f"/imports/{int(run.id)}?processed=1", status_code=303)
            except Exception as exc:
                # Best-effort error persistence.
                try:
                    from fakturek.fakturoid_import import summary_to_json

                    run.status = "error"
                    run.finished_at = utc_now()
                    run.summary_json = summary_to_json({"phase": 25, "error": str(exc)})
                    db.add(run)
                    db.commit()
                except Exception:
                    db.rollback()
                return RedirectResponse(url=f"/imports/{int(run.id)}?error=1", status_code=303)

        @app.get("/imports/{run_id}", response_class=HTMLResponse)
        def import_run_detail(
            run_id: int,
            request: Request,
            db: Session = Depends(get_db),
            uploaded: bool = False,
            duplicate: bool = False,
            processed: bool = False,
            error: bool = False,
        ):
            notice = None
            if uploaded:
                notice = "Soubor byl nahrán."
            if duplicate:
                notice = "Soubor už byl nahrán dříve – tento běh byl znovu použit."
            if processed:
                notice = "Import byl zpracován."
            if error:
                notice = None

            try:
                sid = _current_subject_id()
                run = db.scalar(
                    select(ImportRun)
                    .where(ImportRun.id == int(run_id))
                    .where(ImportRun.subject_id == int(sid))
                    .limit(1)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Export/Import", db_error=str(exc))

            if run is None:
                raise HTTPException(status_code=404, detail="Import run not found")

            summary = None
            preview = None
            preview_error = None
            config = _import_run_config(run)
            try:
                if getattr(run, "summary_json", None):
                    summary = json.loads(str(run.summary_json))
            except Exception:
                summary = None

            if str(getattr(run, "status", "") or "") in {"uploaded", "error"}:
                try:
                    from fakturek.fakturoid_import import preview_import_run

                    preview = preview_import_run(
                        db,
                        run=run,
                        subject_id=int(sid),
                        import_storage_root=import_storage_root,
                    )
                except Exception as exc:
                    preview_error = str(exc)

            return templates.TemplateResponse(
                request,
                "imports/detail.html",
                {
                    "db_enabled": True,
                    "notice": notice,
                    "error": "Import selhal." if error else None,
                    "run": run,
                    "run_source_label": _import_source_label(getattr(run, "source", "")),
                    "summary": summary,
                    "preview": preview,
                    "preview_error": preview_error,
                    "config": config,
                    "can_process": str(getattr(run, "status", "")) in {"uploaded", "error"},
                    "contact_mapping_fields": IMPORT_CONTACT_MAPPING_FIELDS,
                    "contact_conflict_options": IMPORT_CONTACT_CONFLICT_OPTIONS,
                    "invoice_conflict_options": IMPORT_INVOICE_CONFLICT_OPTIONS,
                },
            )
    else:
        @app.get("/imports", response_class=HTMLResponse)
        def imports_page(request: Request):
            return _render_db_disabled(request, title="Export/Import")

        @app.get("/imports/{run_id}", response_class=HTMLResponse)
        def import_run_detail(run_id: int, request: Request):
            return _render_db_disabled(request, title="Export/Import")


    if _db_enabled:
        # --- Contacts ----------------------------------------------------

        CONTACT_TITLE = "Kontakt"
        CONTACT_EDIT_TITLE = "Upravit kontakt"

        INVOICE_TITLE = "Faktura"
        INVOICE_NEW_TITLE = "Nová faktura"
        INVOICE_EDIT_TITLE = "Upravit fakturu"

        # Phase-18: introduce explicit "issued" state.
        ALLOWED_INVOICE_STATUSES = ["draft", "issued", "sent", "paid", "cancelled"]
        BULK_INVOICE_ACTION_OPTIONS: list[tuple[str, str]] = [
            ("paid", "Označit jako zaplacené"),
            ("sent", "Označit jako odeslané"),
            ("revert", "Vrátit o krok zpět"),
        ]

        def _invoice_revert_target(invoice: Invoice | None) -> str | None:
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

        def _invoice_revert_label(invoice: Invoice | None) -> str | None:
            target = _invoice_revert_target(invoice)
            if target == "draft":
                return "Vrátit na koncept"
            if target == "issued":
                return "Vrátit na vystavenou"
            if target == "sent":
                return "Vrátit na odeslanou"
            return None

        def _invoice_page_title_for_type(document_type: str | None, *, mode: str = "detail") -> str:
            normalized = _normalize_invoice_document_type(document_type)
            if mode == "new":
                if normalized == "quote":
                    return "Nová nabídka"
                if normalized == "credit_note":
                    return "Nový dobropis"
                if normalized == "proforma":
                    return "Nová zálohová faktura"
                return INVOICE_NEW_TITLE
            if mode == "edit":
                if normalized == "quote":
                    return "Upravit nabídku"
                if normalized == "credit_note":
                    return "Upravit dobropis"
                if normalized == "proforma":
                    return "Upravit zálohovou fakturu"
                return INVOICE_EDIT_TITLE
            return _invoice_document_type_label(normalized)

        def _new_invoice_submit_label(document_type: str | None) -> str:
            normalized = _normalize_invoice_document_type(document_type)
            if normalized == "invoice":
                return "Vystavit fakturu"
            return "Vytvořit koncept"

        def _new_invoice_page_subtitle(document_type: str | None) -> str:
            normalized = _normalize_invoice_document_type(document_type)
            if normalized == "quote":
                return "Připrav nabídku, kterou pak jedním klikem proměníš na zálohovou nebo ostrou fakturu."
            if normalized == "proforma":
                return "Připrav zálohovou fakturu jako výzvu k úhradě. Číselná řada i PDF se přizpůsobí automaticky."
            if normalized == "invoice":
                return "Vystav fakturu rovnou, nebo si ji ulož jako koncept. Koncept má jen interní DRAFT-ID a finální číslo dostane až při vystavení."
            return "Vytvoř koncept dokladu pohodlně v jednom editoru – bez skákání na detail kvůli položkám."

        def _invoice_list_filter_options() -> list[tuple[str, str]]:
            return [
                ("quote", "Nabídky"),
                ("invoice", "Faktury"),
                ("credit_note", "Dobropisy"),
                ("proforma", "Zálohové faktury"),
            ]

        def _invoice_document_type_filter_value(value: str | None) -> str:
            raw = str(value or "").strip().lower()
            allowed = {option_value for option_value, _option_label in INVOICE_DOCUMENT_TYPE_OPTIONS}
            return raw if raw in allowed else ""

        def _invoice_status_transition_error(*, old_status: str, new_status: str) -> str:
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

        def _apply_invoice_status_transition(
            invoice: Invoice,
            *,
            new_status: str,
            paid_on: date | None = None,
        ) -> tuple[bool, str | None]:
            old_status = str(getattr(invoice, "status", "") or "").strip().lower()
            target = str(new_status or "").strip().lower()
            if old_status == target:
                return False, "Stav faktury už je nastavený."

            revert_target = _invoice_revert_target(invoice)
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
                return False, _invoice_status_transition_error(old_status=old_status, new_status=target)

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
                invoice.paid_on = paid_on or invoice.paid_on or date.today()
                return True, None

            return False, "Neplatný stav faktury."

        def _invoice_manual_payment_note(source: str | None = None) -> str:
            normalized = str(source or "").strip().lower()
            if normalized == "bulk_status":
                return "Ručně označeno jako zaplacené hromadnou úpravou"
            if normalized == "api_status":
                return "Ručně označeno jako zaplacené přes API"
            return "Ručně označeno jako zaplacené"

        def _ensure_manual_invoice_payment(
            db: Session,
            *,
            invoice: Invoice,
            paid_on: date | None,
            source: str | None = None,
        ) -> Payment:
            payment = Payment(
                invoice_id=int(invoice.id),
                paid_on=paid_on or getattr(invoice, "paid_on", None) or date.today(),
                amount_cents=int(getattr(invoice, "total_cents", 0) or 0),
                note=_invoice_manual_payment_note(source),
            )
            db.add(payment)
            db.flush()
            return payment

        def _remove_unlinked_manual_invoice_payments(db: Session, *, invoice: Invoice) -> int:
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

        def _bulk_invoice_action_label(action: str | None) -> str:
            normalized = str(action or "").strip().lower()
            for value, label in BULK_INVOICE_ACTION_OPTIONS:
                if value == normalized:
                    return label
            return "Hromadná úprava"

        def _apply_bulk_invoice_action(
            invoice: Invoice,
            *,
            action: str,
            paid_on: date | None = None,
        ) -> tuple[bool, str | None, str | None]:
            normalized = str(action or "").strip().lower()
            if normalized == "revert":
                target = _invoice_revert_target(invoice)
                if not target:
                    return False, "Doklad nejde vrátit o krok zpět.", None
                changed, error = _apply_invoice_status_transition(invoice, new_status=target)
                return changed, error, target
            if normalized in {"sent", "paid"}:
                changed, error = _apply_invoice_status_transition(
                    invoice,
                    new_status=normalized,
                    paid_on=paid_on if normalized == "paid" else None,
                )
                return changed, error, normalized
            return False, "Neplatná hromadná akce.", None

        # ------------------------------------------------------------------
        # Public invoice sharing (phase-21)
        # ------------------------------------------------------------------
        _PUBLIC_USERNAME_RE = PUBLIC_USERNAME_RE

        def _invoice_public_urls_for_request(
            request: Request,
            *,
            invoice: Invoice,
            subject: Subject | None,
        ) -> dict[str, str] | None:
            public_username = (getattr(subject, "public_username", None) or "").strip().lower() if subject else ""
            token = (getattr(invoice, "public_token", None) or "").strip()
            if str(getattr(invoice, "status", "") or "").strip().lower() == "draft":
                return None
            if not public_username or not token:
                return None
            public_base_url = resolve_public_base_url(
                request=request,
                configured_base_url=getattr(settings, "public_base_url", "") or None,
                trusted_proxy_ips=getattr(settings, "trusted_proxy_ips", ()) or (),
                allow_host_header_fallback=not (settings.app_env == "prod" and settings.auth_required),
            )
            return build_public_invoice_urls(
                base_url=public_base_url,
                public_username=public_username,
                token=token,
                invoice_number=str(getattr(invoice, "number", "") or ""),
                invoice_id=int(getattr(invoice, "id", 0) or 0),
                secret_key=str(settings.public_link_hmac_key or ""),
            )

        def _format_recipient_log_value(*, to_emails: list[str], cc_emails: list[str] | None = None) -> str:
            parts: list[str] = []
            to_list = [str(v or "").strip() for v in list(to_emails or []) if str(v or "").strip()]
            cc_list = [str(v or "").strip() for v in list(cc_emails or []) if str(v or "").strip()]
            if to_list:
                parts.append("To: " + ", ".join(to_list))
            if cc_list:
                parts.append("Cc: " + ", ".join(cc_list))
            return " | ".join(parts)[:255]

        def _normalize_contact_email_input(value: str | None) -> str:
            raw = str(value or "").strip()
            if not raw:
                return ""
            recipients = split_recipients(raw)
            return ", ".join(recipients)

        def _validate_contact_email_input(value: str | None) -> tuple[str, str | None]:
            normalized = _normalize_contact_email_input(value)
            if not normalized:
                return "", None
            recipients = split_recipients(normalized)
            invalid = [addr for addr in recipients if not looks_like_email(addr)]
            if invalid:
                return normalized, "Neplatný e-mail u kontaktu. Více adres odděl čárkou nebo středníkem."
            return normalized, None

        @app.get("/contacts", response_class=HTMLResponse)
        def contacts_list(
            request: Request,
            db: Session = Depends(get_db),
            q: str | None = None,
            page: int = 1,
        ):
            try:
                sid = _current_subject_id()
                q_clean = (q or "").strip()
                filters = [Contact.subject_id == sid]
                if q_clean:
                    like = f"%{q_clean}%"
                    filters.append(
                        or_(
                            Contact.name.like(like),
                            Contact.email.like(like),
                            Contact.phone.like(like),
                            Contact.ico.like(like),
                            Contact.dic.like(like),
                            Contact.city.like(like),
                        )
                    )
                pagination = _build_pagination_payload(
                    request,
                    page=_normalize_page_number(page),
                    per_page=50,
                    total_count=int(db.scalar(select(func.count(Contact.id)).where(*filters)) or 0),
                )
                contacts = db.scalars(
                    select(Contact)
                    .where(*filters)
                    .order_by(Contact.name.asc(), Contact.id.asc())
                    .offset(int(pagination["offset"]))
                    .limit(int(pagination["limit"]))
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Kontakty", db_error=str(exc))
            return templates.TemplateResponse(
                request,
                "contacts/list.html",
                {
                    "contacts": contacts,
                    "filters": {"q": q_clean},
                    "pagination": pagination,
                },
            )

        @app.get("/contacts/export.csv")
        def contacts_export_csv(request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                subject = _load_subject_for_current_session(db)
                subject_slug = _export_subject_slug(subject, subject_id=int(sid))
                rows = _export_contacts_rows(db, subject_id=int(sid))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Kontakty", db_error=str(exc), status_code=500)
            return _csv_attachment_response(
                [
                    "id",
                    "name",
                    "email",
                    "phone",
                    "street",
                    "city",
                    "zip",
                    "country",
                    "ico",
                    "dic",
                    "external_source",
                    "external_id",
                    "created_at",
                    "updated_at",
                ],
                rows,
                filename=f"{subject_slug}-contacts.csv",
            )

        @app.get("/contacts/new", response_class=HTMLResponse)
        def contacts_new(request: Request, db: Session = Depends(get_db)):
            # Support prefill via query params (used by the "Načíst z ARES" button).
            qp = request.query_params
            prefill = {
                "name": (qp.get("name") or "").strip(),
                "email": (qp.get("email") or "").strip(),
                "phone": (qp.get("phone") or "").strip(),
                "street": (qp.get("street") or "").strip(),
                "city": (qp.get("city") or "").strip(),
                "zip": (qp.get("zip") or "").strip(),
                "country": _normalize_contact_country(qp.get("country")),
                "ico": (qp.get("ico") or "").strip(),
                "dic": (qp.get("dic") or "").strip(),
                "fixed_variable_symbol": _normalize_variable_symbol(qp.get("fixed_variable_symbol")),
                "registry_auto_update": str(qp.get("registry_auto_update") or "1"),
            }

            info: str | None = None
            error: str | None = None

            lookup = (qp.get("lookup") or "").strip().lower()
            country = _normalize_contact_country(prefill.get("country"))

            # CZ lookup (ARES)
            if (lookup == "ares" or (lookup == "" and prefill["ico"] and country == "CZ")) and prefill["ico"]:
                from fakturek.company_lookup import (
                    CompanyLookupError,
                    lookup_cz_company_prefill_with_cache,
                )
                from fakturek.settings import get_settings

                settings = get_settings()
                try:
                    company, source = lookup_cz_company_prefill_with_cache(
                        db,
                        prefill["ico"],
                        base_url=settings.ares_base_url,
                        timeout_seconds=settings.ares_timeout_seconds,
                        cache_ttl_days=settings.company_lookup_cache_ttl_days,
                    )
                    # Overwrite selected fields with authoritative registry data.
                    if company.name:
                        prefill["name"] = company.name
                    prefill["street"] = company.street
                    prefill["city"] = company.city
                    prefill["zip"] = company.zip
                    prefill["country"] = company.country
                    prefill["ico"] = company.ico
                    prefill["dic"] = company.dic

                    info = "Načteno z ARES." if source == "live" else "Načteno z ARES (cache)."
                except CompanyLookupError as exc:
                    error = str(exc)

            # SK lookup (RPO + ORSR fallback)
            if (
                error is None
                and prefill["ico"]
                and (lookup in {"rpo", "sk"} or (lookup == "" and country == "SK"))
            ):
                from fakturek.company_lookup import (
                    CompanyLookupError,
                    lookup_sk_company_prefill_with_cache,
                )
                from fakturek.settings import get_settings

                settings = get_settings()
                try:
                    company, source, provider = lookup_sk_company_prefill_with_cache(
                        db,
                        prefill["ico"],
                        rpo_base_url=settings.sk_rpo_base_url,
                        rpo_timeout_seconds=settings.sk_rpo_timeout_seconds,
                        orsr_base_url=settings.sk_orsr_base_url,
                        orsr_timeout_seconds=settings.sk_orsr_timeout_seconds,
                        cache_ttl_days=settings.company_lookup_cache_ttl_days,
                    )

                    if company.name:
                        prefill["name"] = company.name
                    prefill["street"] = company.street
                    prefill["city"] = company.city
                    prefill["zip"] = company.zip
                    prefill["country"] = company.country or "SK"
                    prefill["ico"] = company.ico
                    prefill["dic"] = company.dic

                    label = "RPO" if provider == "rpo" else "ORSR"
                    info = (
                        f"Načteno z {label}." if source == "live" else f"Načteno z {label} (cache)."
                    )
                except CompanyLookupError as exc:
                    error = str(exc)

            return templates.TemplateResponse(
                request,
                "contacts/new.html",
                {
                    "prefill": prefill,
                    "error": error,
                    "info": info,
                    **_contact_country_template_context(prefill.get("country")),
                },
            )

        @app.post("/contacts/new")
        async def contacts_create(request: Request, db: Session = Depends(get_db)):
            form = await request.form()

            prefill = {k: (form.get(k) or "").strip() for k in [
                "name",
                "email",
                "phone",
                "street",
                "city",
                "zip",
                "country",
                "ico",
                "dic",
            ]}
            prefill["email"] = _normalize_contact_email_input(prefill.get("email"))
            prefill["fixed_variable_symbol"] = _normalize_variable_symbol(form.get("fixed_variable_symbol"))
            prefill["registry_auto_update"] = "1" if str(form.get("registry_auto_update") or "").strip().lower() in {"1", "true", "on", "yes"} else ""

            name = (form.get("name") or "").strip()
            if not name:
                if not prefill.get("country"):
                    prefill["country"] = "CZ"
                return templates.TemplateResponse(
                    request,
                    "contacts/new.html",
                    {"error": "Jméno je povinné.", "prefill": prefill, **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            normalized_contact_email, email_error = _validate_contact_email_input(form.get("email"))
            if email_error:
                if not prefill.get("country"):
                    prefill["country"] = "CZ"
                return templates.TemplateResponse(
                    request,
                    "contacts/new.html",
                    {"error": email_error, "prefill": prefill, **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            raw_fixed_vs = str(form.get("fixed_variable_symbol") or "").strip()
            normalized_fixed_vs = _normalize_variable_symbol(raw_fixed_vs)
            if raw_fixed_vs and not normalized_fixed_vs:
                if not prefill.get("country"):
                    prefill["country"] = "CZ"
                return templates.TemplateResponse(
                    request,
                    "contacts/new.html",
                    {"error": "Pevný VS musí obsahovat číslice.", "prefill": prefill, **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )
            if len(normalized_fixed_vs) > 10:
                if not prefill.get("country"):
                    prefill["country"] = "CZ"
                return templates.TemplateResponse(
                    request,
                    "contacts/new.html",
                    {"error": "Pevný VS může mít maximálně 10 číslic.", "prefill": prefill, **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            sid = _current_subject_id()
            contact = Contact(
                subject_id=sid,
                name=name,
                email=normalized_contact_email or None,
                phone=(form.get("phone") or "").strip() or None,
                street=(form.get("street") or "").strip() or None,
                city=(form.get("city") or "").strip() or None,
                zip=(form.get("zip") or "").strip() or None,
                country=_normalize_contact_country(form.get("country")),
                ico=(form.get("ico") or "").strip() or None,
                dic=(form.get("dic") or "").strip() or None,
                fixed_variable_symbol=normalized_fixed_vs or None,
                registry_auto_update=str(form.get("registry_auto_update") or "").strip().lower() in {"1", "true", "on", "yes"},
            )
            db.add(contact)
            try:
                db.commit()
                db.refresh(contact)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Nový kontakt", db_error=str(exc))

            return RedirectResponse(url=f"/contacts/{contact.id}", status_code=303)

        @app.get("/contacts/{contact_id}", response_class=HTMLResponse)
        def contacts_detail(contact_id: int, request: Request, db: Session = Depends(get_db), notice: str | None = None, error: str | None = None):
            sid = _current_subject_id()
            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == contact_id)
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Kontakt", db_error=str(exc))
            if contact is None:
                return JSONResponse(status_code=404, content={"detail": "Contact not found"})

            try:
                invoices = db.scalars(
                    select(Invoice)
                    .where(Invoice.contact_id == contact_id)
                    .where(Invoice.subject_id == sid)
                    .order_by(*_invoice_newest_first_ordering())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Kontakt", db_error=str(exc))

            return templates.TemplateResponse(
                request,
                "contacts/detail.html",
                {
                    "contact": contact,
                    "contact_email_list": split_recipients((getattr(contact, "email", "") or "").strip()),
                    "invoices": invoices,
                    "notice": notice,
                    "error": error,
                },
            )

        @app.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
        def contacts_edit(contact_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == contact_id)
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=CONTACT_EDIT_TITLE, db_error=str(exc))
            if contact is None:
                return JSONResponse(status_code=404, content={"detail": "Contact not found"})

            qp = request.query_params
            prefill = {
                "name": (qp.get("name") or contact.name or "").strip(),
                "email": (qp.get("email") or contact.email or "").strip(),
                "phone": (qp.get("phone") or contact.phone or "").strip(),
                "street": (qp.get("street") or contact.street or "").strip(),
                "city": (qp.get("city") or contact.city or "").strip(),
                "zip": (qp.get("zip") or contact.zip or "").strip(),
                "country": _normalize_contact_country(qp.get("country") or contact.country),
                "ico": (qp.get("ico") or contact.ico or "").strip(),
                "dic": (qp.get("dic") or contact.dic or "").strip(),
                "fixed_variable_symbol": _normalize_variable_symbol(qp.get("fixed_variable_symbol") or contact.fixed_variable_symbol),
                "registry_auto_update": "1" if bool(getattr(contact, "registry_auto_update", True)) else "",
            }

            info: str | None = None
            error: str | None = None
            lookup = (qp.get("lookup") or "").strip().lower()
            if lookup == "ares":
                from fakturek.company_lookup import (
                    CompanyLookupError,
                    lookup_cz_company_prefill_with_cache,
                )
                from fakturek.settings import get_settings

                settings = get_settings()
                try:
                    company, source = lookup_cz_company_prefill_with_cache(
                        db,
                        prefill["ico"],
                        base_url=settings.ares_base_url,
                        timeout_seconds=settings.ares_timeout_seconds,
                        cache_ttl_days=settings.company_lookup_cache_ttl_days,
                    )
                    if company.name:
                        prefill["name"] = company.name
                    prefill["street"] = company.street
                    prefill["city"] = company.city
                    prefill["zip"] = company.zip
                    prefill["country"] = company.country
                    prefill["ico"] = company.ico
                    prefill["dic"] = company.dic

                    info = "Načteno z ARES." if source == "live" else "Načteno z ARES (cache)."
                except CompanyLookupError as exc:
                    error = str(exc)

            if lookup in {"rpo", "sk"} and prefill.get("ico"):
                from fakturek.company_lookup import (
                    CompanyLookupError,
                    lookup_sk_company_prefill_with_cache,
                )
                from fakturek.settings import get_settings

                settings = get_settings()
                try:
                    company, source, provider = lookup_sk_company_prefill_with_cache(
                        db,
                        prefill["ico"],
                        rpo_base_url=settings.sk_rpo_base_url,
                        rpo_timeout_seconds=settings.sk_rpo_timeout_seconds,
                        orsr_base_url=settings.sk_orsr_base_url,
                        orsr_timeout_seconds=settings.sk_orsr_timeout_seconds,
                        cache_ttl_days=settings.company_lookup_cache_ttl_days,
                    )
                    if company.name:
                        prefill["name"] = company.name
                    prefill["street"] = company.street
                    prefill["city"] = company.city
                    prefill["zip"] = company.zip
                    prefill["country"] = company.country or "SK"
                    prefill["ico"] = company.ico
                    prefill["dic"] = company.dic

                    label = "RPO" if provider == "rpo" else "ORSR"
                    info = (
                        f"Načteno z {label}." if source == "live" else f"Načteno z {label} (cache)."
                    )
                except CompanyLookupError as exc:
                    error = str(exc)

            return templates.TemplateResponse(
                request,
                "contacts/edit.html",
                {
                    "contact": contact,
                    "prefill": prefill,
                    "error": error,
                    "info": info,
                    **_contact_country_template_context(prefill.get("country")),
                },
            )

        @app.post("/contacts/{contact_id}/edit")
        async def contacts_update(contact_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()

            name = (form.get("name") or "").strip()

            sid = _current_subject_id()

            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == contact_id)
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=CONTACT_EDIT_TITLE, db_error=str(exc))
            if contact is None:
                return JSONResponse(status_code=404, content={"detail": "Contact not found"})

            prefill = {k: (form.get(k) or "").strip() for k in [
                "name",
                "email",
                "phone",
                "street",
                "city",
                "zip",
                "country",
                "ico",
                "dic",
            ]}
            prefill["email"] = _normalize_contact_email_input(prefill.get("email"))
            prefill["fixed_variable_symbol"] = _normalize_variable_symbol(form.get("fixed_variable_symbol"))
            prefill["registry_auto_update"] = "1" if str(form.get("registry_auto_update") or "").strip().lower() in {"1", "true", "on", "yes"} else ""

            if not name:
                return templates.TemplateResponse(
                    request,
                    "contacts/edit.html",
                    {"contact": contact, "prefill": prefill, "error": "Jméno je povinné.", **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            normalized_contact_email, email_error = _validate_contact_email_input(form.get("email"))
            if email_error:
                return templates.TemplateResponse(
                    request,
                    "contacts/edit.html",
                    {"contact": contact, "prefill": prefill, "error": email_error, **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            raw_fixed_vs = str(form.get("fixed_variable_symbol") or "").strip()
            normalized_fixed_vs = _normalize_variable_symbol(raw_fixed_vs)
            if raw_fixed_vs and not normalized_fixed_vs:
                return templates.TemplateResponse(
                    request,
                    "contacts/edit.html",
                    {"contact": contact, "prefill": prefill, "error": "Pevný VS musí obsahovat číslice.", **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )
            if len(normalized_fixed_vs) > 10:
                return templates.TemplateResponse(
                    request,
                    "contacts/edit.html",
                    {"contact": contact, "prefill": prefill, "error": "Pevný VS může mít maximálně 10 číslic.", **_contact_country_template_context(prefill.get("country"))},
                    status_code=400,
                )

            contact.name = name
            contact.email = normalized_contact_email or None
            contact.phone = (form.get("phone") or "").strip() or None
            contact.street = (form.get("street") or "").strip() or None
            contact.city = (form.get("city") or "").strip() or None
            contact.zip = (form.get("zip") or "").strip() or None
            contact.country = _normalize_contact_country(form.get("country"))
            contact.ico = (form.get("ico") or "").strip() or None
            contact.dic = (form.get("dic") or "").strip() or None
            contact.fixed_variable_symbol = normalized_fixed_vs or None
            contact.registry_auto_update = str(form.get("registry_auto_update") or "").strip().lower() in {"1", "true", "on", "yes"}

            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=CONTACT_EDIT_TITLE, db_error=str(exc))

            return RedirectResponse(url=f"/contacts/{contact.id}", status_code=303)

        @app.post("/contacts/{contact_id}/registry-sync")
        async def contacts_registry_sync(contact_id: int, request: Request, db: Session = Depends(get_db)):
            await _verify_csrf(request)
            sid = _current_subject_id()
            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == int(contact_id))
                    .where(Contact.subject_id == int(sid))
                    .limit(1)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))
            if contact is None:
                return JSONResponse(status_code=404, content={"detail": "Contact not found"})
            try:
                result = sync_contact_from_registry(db, contact, force=True)
                _audit_log(
                    db,
                    request=request,
                    action="contact_registry_sync",
                    entity_type="contact",
                    entity_id=int(contact.id),
                    data=result,
                )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))

            if result.get("error"):
                return RedirectResponse(url=_with_query_params(f"/contacts/{int(contact.id)}", error=str(result.get("error") or "Kontrola registru selhala.")), status_code=303)
            if result.get("changed"):
                return RedirectResponse(url=_with_query_params(f"/contacts/{int(contact.id)}", notice="Kontakt byl aktualizovaný podle registru."), status_code=303)
            return RedirectResponse(url=_with_query_params(f"/contacts/{int(contact.id)}", notice="Kontrola registru proběhla, změny nejsou."), status_code=303)

        @app.post("/contacts/{contact_id}/delete")
        def contacts_delete(contact_id: int, request: Request, db: Session = Depends(get_db)):
            # Conservative deletion: refuse to delete when contact has invoices.
            sid = _current_subject_id()
            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == contact_id)
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))
            if contact is None:
                return JSONResponse(status_code=404, content={"detail": "Contact not found"})

            try:
                inv_cnt = db.scalar(
                    select(func.count(Invoice.id))
                    .where(Invoice.contact_id == contact_id)
                    .where(Invoice.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))

            if int(inv_cnt or 0) > 0:
                # Render detail with an error.
                try:
                    invoices = db.scalars(
                        select(Invoice)
                        .where(Invoice.contact_id == contact_id)
                        .where(Invoice.subject_id == sid)
                        .order_by(Invoice.id.desc())
                    ).all()
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))

                return templates.TemplateResponse(
                    request,
                    "contacts/detail.html",
                    {
                        "contact": contact,
                        "invoices": invoices,
                        "error": "Kontakt nelze smazat – existují na něj navázané faktury.",
                    },
                    status_code=400,
                )

            db.delete(contact)
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=CONTACT_TITLE, db_error=str(exc))

            return RedirectResponse(url="/contacts", status_code=303)

        # --- Invoices ----------------------------------------------------

        def _render_invoice_detail(
            *,
            request: Request,
            db: Session,
            invoice_id: int,
            error: str | None = None,
            notice: str | None = None,
            prefill_item: dict | None = None,
            prefill_email: dict | None = None,
            prefill_reminder: dict | None = None,
            status_code: int = 200,
            subject_override: Subject | None = None,
        ):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                    .options(selectinload(Invoice.emails))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == invoice_id)
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))

            subject = subject_override or _load_subject_for_current_session(db)
            invoice_language = _normalize_invoice_language(getattr(invoice, "invoice_language", None))
            invoice_i18n = _invoice_texts(invoice_language)
            payment_account = _invoice_bank_account_payload(invoice, subject=subject)
            payment_method, payment_method_label = _effective_invoice_payment_context(
                db,
                invoice=invoice,
                language=invoice_language,
            )
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            document_label = _invoice_document_type_label(document_type, invoice_language)
            document_kicker = _invoice_document_type_kicker(document_type, invoice_language)
            document_detail_label = _invoice_document_type_detail_label(document_type, invoice_language)
            source_invoice_number = _invoice_source_invoice_number(db, invoice=invoice)
            variable_symbol = _invoice_variable_symbol(invoice, contact=invoice.contact)
            footer_mode, footer_text = _resolve_invoice_footer(
                subject=subject,
                footer_mode=getattr(invoice, "footer_mode", None),
                footer_text=getattr(invoice, "footer_text", None),
                language=invoice_language,
            )
            display_items = _items_with_rounding_line(list(items), invoice)
            try:
                parties = _load_invoice_parties_map(db, invoice_id=int(invoice.id))
            except SQLAlchemyError:
                parties = {}
            buyer_party = parties.get("buyer")
            invoice_buyer = {
                **_party_payload_from_contact(invoice.contact),
                **(
                    {k: str(getattr(buyer_party, k, "") or "") for k in _party_payload_from_contact(None).keys()}
                    if buyer_party is not None
                    else {}
                ),
            }
            is_vat_payer, _default_currency = _subject_flags(db)
            items_total_cents = sum(int(it.line_total_cents or 0) for it in items)
            discount_cents = int(getattr(invoice, "discount_cents", 0) or 0)
            rounding_adj_cents = int(invoice.rounding_adjustment_cents or 0)
            show_vat = bool(is_vat_payer) and _invoice_has_vat(display_items)
            vat_summary = _invoice_vat_summary(list(items)) if bool(is_vat_payer) else {"rows": [], "net_cents": 0, "vat_cents": 0, "gross_cents": 0}
            vat_classification = _invoice_vat_classification(invoice=invoice, subject=subject, vat_summary=vat_summary) if bool(is_vat_payer) else {}
            current_link = _user_subject_link(
                db,
                user_id=_current_user_id_or_none(request),
                subject_id=int(sid),
            ) if settings.auth_required else None
            can_edit = True if not settings.auth_required else bool(getattr(current_link, "can_edit", False))
            can_issue = True if not settings.auth_required else bool(getattr(current_link, "can_issue", False))
            can_export = True if not settings.auth_required else bool(getattr(current_link, "can_export", False))
            current_status = str(invoice.status or "").strip().lower()
            can_delete = bool(can_edit)
            can_cancel = bool(can_edit) and current_status in {"issued", "sent", "paid"}
            revert_status = _invoice_revert_target(invoice)
            revert_status_label = _invoice_revert_label(invoice)
            conversion_targets = _invoice_conversion_targets(document_type)
            credit_summary = (
                _invoice_related_credit_note_summary(db, invoice_id=int(invoice.id), include_drafts=True)
                if document_type == "invoice"
                else {"items": [], "credited_total_cents": 0}
            )
            remaining_after_credit_cents = (
                max(int(getattr(invoice, "total_cents", 0) or 0) - int(credit_summary.get("credited_total_cents") or 0), 0)
                if document_type == "invoice"
                else 0
            )

            public_url: str | None = None
            public_pdf_url: str | None = None
            public_pdf_download_url: str | None = None
            public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
            public_username = (getattr(subject, "public_username", None) or "").strip() if subject else ""
            if public_urls:
                public_url = public_urls["view"]
                public_pdf_url = public_urls["pdf"]
                public_pdf_download_url = public_urls["pdf_download"]
            public_view_count = int(getattr(invoice, "public_view_count", 0) or 0)
            public_first_viewed_at = getattr(invoice, "public_first_viewed_at", None)
            public_last_viewed_at = getattr(invoice, "public_last_viewed_at", None)

            payment_qr_codes: list[PaymentQRCode] = []

            mail_ctx = _mail_identity_context(db, subject=subject, request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "").strip()
            signature_name = str(mail_ctx.get("signature_name") or "").strip()
            copy_to_self_email = str(mail_ctx.get("copy_to_self_email") or "").strip()
            contact_email_raw = (getattr(invoice.contact, "email", "") or "").strip()
            contact_email_list = split_recipients(contact_email_raw)
            contact_email_joined = ", ".join(contact_email_list)

            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )

            smtp_ready = smtp_is_configured(smtp_cfg) and looks_like_email(from_email)

            today_local = date.today()
            is_overdue = bool(invoice.due_date < today_local) and current_status not in {"paid", "cancelled"}
            days_overdue = (today_local - invoice.due_date).days if is_overdue else 0
            display_bank_account = payment_account.display if payment_account is not None else ""

            if prefill_reminder is None and str(invoice.status or "").strip().lower() != "draft" and is_overdue:
                total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
                subj_line = f"{_invoice_text('reminder_subject_prefix', invoice_language)}: {document_label} {invoice.number}"
                body_lines = [
                    _invoice_text("email_hello", invoice_language),
                    "",
                    f"{_invoice_text('reminder_intro', invoice_language)} {document_label.lower()} {invoice.number} {_invoice_text('reminder_amount', invoice_language)} {total_str}.",
                    f"{_invoice_text('email_due', invoice_language)}: {invoice.due_date} ({days_overdue} {_invoice_text('reminder_overdue', invoice_language)}).",
                    "",
                    _invoice_text("reminder_request", invoice_language),
                ]
                if display_bank_account:
                    body_lines += ["", f"{_invoice_text('email_account_number', invoice_language)}: {display_bank_account}", f"{_invoice_text('email_reference', invoice_language)}: {variable_symbol or invoice.number}"]
                if public_url:
                    body_lines += ["", public_url]
                body_lines += ["", _invoice_text("email_best_regards", invoice_language), signature_name or from_name or ""]

                prefill_reminder = {
                    "to_email": contact_email_joined,
                    "cc_email": "",
                    "subject": subj_line,
                    "body": "\n".join([ln for ln in body_lines if ln is not None]),
                    "attach_pdf": True,
                    "include_public_link": bool(public_url),
                    "days_overdue": int(days_overdue),
                }

            if prefill_email is None and str(invoice.status or "").strip().lower() != "draft":
                total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
                subj_line = _invoice_document_email_subject(document_type, invoice.number, invoice_language)
                body_lines = [
                    _invoice_text("email_hello", invoice_language),
                    "",
                    f"{_invoice_text('email_attached', invoice_language)} {document_label.lower()} {invoice.number} {_invoice_text('reminder_amount', invoice_language)} {total_str}.",
                    f"{_invoice_text('email_due', invoice_language)}: {invoice.due_date}.",
                ]
                if public_url:
                    body_lines += ["", public_url]
                body_lines += ["", _invoice_text("email_best_regards", invoice_language), signature_name or from_name or ""]
                prefill_email = {
                    "to_email": contact_email_joined,
                    "cc_email": "",
                    "subject": subj_line,
                    "body": "\n".join([ln for ln in body_lines if ln is not None]),
                    "attach_pdf": True,
                    "include_public_link": bool(public_url),
                }

            emails_log = []
            try:
                emails_log = sorted(
                    list(getattr(invoice, "emails", []) or []),
                    key=lambda e: getattr(e, "created_at", datetime.min),
                    reverse=True,
                )
            except Exception:
                emails_log = list(getattr(invoice, "emails", []) or [])

            audit_entries = _load_invoice_audit_entries(db, invoice_id=int(invoice.id), subject_id=int(sid))

            return templates.TemplateResponse(
                request,
                "invoices/detail.html",
                {
                    "invoice": invoice,
                    "invoice_buyer": invoice_buyer,
                    "items": list(display_items),
                    "items_total_cents": items_total_cents,
                    "discount_cents": discount_cents,
                    "rounding_adjustment_cents": rounding_adj_cents,
                    "is_vat_payer": bool(is_vat_payer),
                    "show_vat": bool(show_vat),
                    "vat_summary": vat_summary,
                    "vat_classification": vat_classification,
                    "can_edit": bool(can_edit),
                    "can_issue": bool(can_issue),
                    "can_export": bool(can_export),
                    "can_delete": bool(can_delete),
                    "can_cancel": bool(can_cancel),
                    "revert_status": revert_status,
                    "revert_status_label": revert_status_label,
                    "error": error,
                    "notice": notice,
                    "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                    "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                    "prefill_email": prefill_email,
                    "prefill_reminder": prefill_reminder,
                    "emails_log": emails_log,
                    "smtp_ready": bool(smtp_ready),
                    "smtp_from_email": from_email or None,
                    "smtp_from_name": from_name or None,
                    "mail_signature_name": signature_name or None,
                    "copy_to_self_email": copy_to_self_email or None,
                    "smtp_host": (settings.smtp_host or "").strip() or None,
                    "smtp_port": int(settings.smtp_port or 0) or None,
                    "contact_email_list": contact_email_list,
                    "contact_email_joined": contact_email_joined,
                    "today": date.today(),
                    "days_overdue": int(days_overdue),
                    "allowed_statuses": ALLOWED_INVOICE_STATUSES,
                    "public_username": public_username or None,
                    "public_url": public_url,
                    "public_pdf_url": public_pdf_url,
                    "public_pdf_download_url": public_pdf_download_url,
                    "public_view_count": public_view_count,
                    "public_first_viewed_at": public_first_viewed_at,
                    "public_last_viewed_at": public_last_viewed_at,
                    "payment_account": payment_account,
                    "payment_qr_codes": payment_qr_codes,
                    "taxable_supply_date": _invoice_taxable_supply_date(invoice),
                    "payment_method": payment_method,
                    "payment_method_label": payment_method_label,
                    "document_type": document_type,
                    "document_label": document_label,
                    "document_kicker": document_kicker,
                    "document_detail_label": document_detail_label,
                    "invoice_language": invoice_language,
                    "invoice_i18n": invoice_i18n,
                    "status_label": _invoice_status_label_for_lang(invoice.status, invoice_language),
                    "source_invoice_number": source_invoice_number,
                    "variable_symbol": variable_symbol,
                    "footer_mode": footer_mode,
                    "footer_text": footer_text,
                    "audit_entries": audit_entries,
                    "conversion_targets": conversion_targets,
                    "credit_summary": credit_summary,
                    "remaining_after_credit_cents": remaining_after_credit_cents,
                    "recurring_prefill": {
                        "name": f"{document_label} {invoice.number}".strip(),
                        "interval_unit": "month",
                        "interval_count": 1,
                        "next_issue_date": _add_recurrence_step(date.today(), interval_unit="month", interval_count=1).isoformat(),
                        "due_in_days": max(0, (invoice.due_date - invoice.issue_date).days),
                    },
                    "next_url": f"/invoices/{invoice_id}",
                },
                status_code=status_code,
            )

        def _build_invoice_print_context(
            db: Session,
            *,
            invoice: Invoice,
            items: list[InvoiceItem],
            subject_override: Subject | None = None,
        ) -> dict:
            """Build context for invoice print/PDF template.

            Prefers snapshot parties (invoice_parties) and falls back to the
            current subject/contact.
            """

            invoice_id = int(invoice.id)

            try:
                parties = _load_invoice_parties_map(db, invoice_id=invoice_id)
            except SQLAlchemyError:
                parties = {}

            subject = subject_override or _load_subject_for_current_session(db)
            invoice_language = _normalize_invoice_language(getattr(invoice, "invoice_language", None))
            invoice_style = _normalize_invoice_style(
                getattr(invoice, "invoice_style", None) or _default_invoice_style(subject)
            )
            invoice_i18n = _invoice_texts(invoice_language)

            buyer_dict = {**_party_payload_from_contact(invoice.contact), "bank_account": ""}
            seller_dict = _party_payload_from_subject(subject)

            buyer_party = parties.get("buyer")
            seller_party = parties.get("seller")
            if buyer_party is not None:
                buyer_dict = {
                    **buyer_dict,
                    **{k: str(getattr(buyer_party, k, "") or "") for k in _party_payload_from_contact(None).keys()},
                }

            if seller_party is not None:
                seller_dict = {
                    **seller_dict,
                    **{k: str(getattr(seller_party, k, "") or "") for k in _party_payload_from_subject(None).keys()},
                }

            payment_account = _invoice_bank_account_payload(invoice, subject=subject)
            payment_method, payment_method_label = _effective_invoice_payment_context(
                db,
                invoice=invoice,
                language=invoice_language,
            )
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            document_label = _invoice_document_type_label(document_type, invoice_language)
            document_kicker = _invoice_document_type_kicker(document_type, invoice_language)
            source_invoice_number = _invoice_source_invoice_number(db, invoice=invoice)
            variable_symbol = _invoice_variable_symbol(invoice, contact=invoice.contact)
            footer_mode, footer_text = _resolve_invoice_footer(
                subject=subject,
                footer_mode=getattr(invoice, "footer_mode", None),
                footer_text=getattr(invoice, "footer_text", None),
                language=invoice_language,
            )
            seller_dict = {
                **seller_dict,
                "bank_account": payment_account.display if payment_account is not None else "",
                "bank_account_label": payment_account.label if payment_account is not None else "",
            }

            items_total_cents = sum(int(it.line_total_cents or 0) for it in items)
            discount_cents = int(getattr(invoice, "discount_cents", 0) or 0)
            rounding_adj_cents = int(invoice.rounding_adjustment_cents or 0)
            display_items = _items_with_rounding_line(list(items), invoice)
            payment_qr_codes: list[PaymentQRCode] = []
            seller_country = (str(seller_dict.get("country") or getattr(subject, "country", None) or "") or "").strip().upper()
            if payment_method == "bank_transfer" and payment_account is not None and int(invoice.total_cents or 0) > 0:
                try:
                    payment_qr_codes = build_payment_qr_codes(
                        account=payment_account,
                        amount_cents=int(invoice.total_cents or 0),
                        currency=str(invoice.currency or "CZK"),
                        beneficiary_name=(seller_dict.get("name") or "").strip(),
                        invoice_number=str(invoice.number or ""),
                        variable_symbol=variable_symbol,
                        due_date=None,
                        subject_country=seller_country,
                    )
                except Exception:
                    payment_qr_codes = []

            return {
                "invoice": invoice,
                "document_type": document_type,
                "document_label": document_label,
                "document_kicker": document_kicker,
                "source_invoice_number": source_invoice_number,
                "items": list(display_items),
                "taxable_supply_date": _invoice_taxable_supply_date(invoice),
                "seller": seller_dict,
                "buyer": buyer_dict,
                "items_total_cents": items_total_cents,
                "discount_cents": discount_cents,
                "rounding_adjustment_cents": rounding_adj_cents,
                "show_vat": bool(getattr(subject, "is_vat_payer", False)) and _invoice_has_vat(display_items),
                "is_vat_payer": bool(getattr(subject, "is_vat_payer", False)),
                "payment_account": payment_account,
                "payment_qr_codes": payment_qr_codes,
                "payment_method": payment_method,
                "payment_method_label": payment_method_label,
                "status_label": _invoice_status_label_for_lang(invoice.status, invoice_language),
                "invoice_language": invoice_language,
                "invoice_style": invoice_style,
                "invoice_i18n": invoice_i18n,
                "variable_symbol": variable_symbol,
                "footer_mode": footer_mode,
                "footer_text": footer_text,
            }

        def _invoice_pdf_data_from_context(
            *,
            invoice: Invoice,
            ctx: dict,
        ) -> InvoicePDFData:
            items_total_cents = int(ctx.get("items_total_cents") or 0)
            discount_cents = int(ctx.get("discount_cents") or 0)
            rounding_adj_cents = int(ctx.get("rounding_adjustment_cents") or 0)
            total_cents = int(items_total_cents - discount_cents + rounding_adj_cents)
            buyer_dict = {k: str(v or "") for k, v in dict(ctx.get("buyer") or {}).items()}
            seller_dict = {k: str(v or "") for k, v in dict(ctx.get("seller") or {}).items()}
            payment_account_obj = ctx.get("payment_account")
            payment_account_raw_iban = str(getattr(payment_account_obj, "iban", "") or "")
            payment_account_dict = {
                "label": str(getattr(payment_account_obj, "label", "") or ""),
                "number": str(getattr(payment_account_obj, "number", "") or ""),
                "display": str(getattr(payment_account_obj, "display", "") or ""),
                "iban": payment_account_raw_iban,
                "iban_display": str(
                    getattr(payment_account_obj, "iban_display", "")
                    or format_iban_for_display(payment_account_raw_iban)
                ),
                "bic": str(getattr(payment_account_obj, "bic", "") or ""),
                "country": str(getattr(payment_account_obj, "country", "") or ""),
            } if payment_account_obj is not None else {}
            payment_qr_codes = [
                {
                    "kind": str(getattr(qr, "kind", "") or ""),
                    "title": str(getattr(qr, "title", "") or ""),
                    "payload": str(getattr(qr, "payload", "") or ""),
                    "image_data_uri": str(getattr(qr, "image_data_uri", "") or ""),
                }
                for qr in list(ctx.get("payment_qr_codes") or [])
            ]
            return InvoicePDFData(
                number=invoice.number,
                status=invoice.status,
                language=str(ctx.get("invoice_language") or getattr(invoice, "invoice_language", None) or "cs"),
                invoice_style=str(ctx.get("invoice_style") or getattr(invoice, "invoice_style", None) or "modern"),
                document_type=str(ctx.get("document_type") or getattr(invoice, "document_type", "") or "invoice"),
                document_label=str(ctx.get("document_label") or "Faktura"),
                issue_date=invoice.issue_date,
                taxable_supply_date=_invoice_taxable_supply_date(invoice),
                due_date=invoice.due_date,
                currency=invoice.currency,
                items_total_cents=int(items_total_cents),
                discount_cents=int(discount_cents),
                rounding_adjustment_cents=int(rounding_adj_cents),
                total_cents=int(total_cents),
                notes=invoice.notes,
                payment_method=str(ctx.get("payment_method") or getattr(invoice, "payment_method", "") or "bank_transfer"),
                variable_symbol=str(ctx.get("variable_symbol") or _invoice_variable_symbol(invoice)),
                footer_text=str(ctx.get("footer_text") or ""),
                source_invoice_number=str(ctx.get("source_invoice_number") or ""),
                issuer=seller_dict,
                customer=buyer_dict,
                items=[
                    {
                        "description": getattr(it, "description", ""),
                        "quantity": str(getattr(it, "quantity", "1")),
                        "unit": _normalize_invoice_item_unit(getattr(it, "unit", "")),
                        "unit_price_cents": int(getattr(it, "unit_price_cents", 0) or 0),
                        "vat_rate": str(getattr(it, "vat_rate", "0")),
                        "line_total_cents": int(getattr(it, "line_total_cents", 0) or 0),
                    }
                    for it in list(ctx.get("items") or [])
                ],
                payment_account=payment_account_dict,
                payment_qr_codes=payment_qr_codes,
            )

        def _public_back_url_for_invoice(
            request: Request,
            *,
            db: Session,
            invoice: Invoice,
        ) -> str | None:
            user_id = _current_user_id_or_none(request)
            invoice_subject_id = int(getattr(invoice, "subject_id", 0) or 0)
            if user_id is None or invoice_subject_id <= 0:
                return None
            if not _user_can_view_subject(db, user_id=user_id, subject_id=invoice_subject_id):
                return None
            target = f"/invoices/{int(invoice.id)}"
            try:
                current_sid = int(request.session.get("subject_id") or 0)
            except Exception:
                current_sid = 0
            if current_sid == invoice_subject_id:
                return target
            return _subject_switch_url(
                request,
                subject_id=invoice_subject_id,
                next_url=target,
            )

        def _persist_invoice_pdf_best_effort(
            db: Session,
            *,
            invoice: Invoice,
            pdf_bytes: bytes,
        ) -> None:
            """Persist issued PDF on disk and store metadata on the invoice.

            Best-effort: never raises to the caller.
            """

            try:
                if str(invoice.status or "") == "draft":
                    return

                relpath, digest = persist_pdf_bytes(
                    storage_root=pdf_storage_root,
                    subject_id=int(invoice.subject_id),
                    invoice_id=int(invoice.id),
                    invoice_number=str(invoice.number or ""),
                    pdf_bytes=bytes(pdf_bytes),
                )

                invoice.pdf_path = relpath
                invoice.pdf_hash = digest
                invoice.pdf_generated_at = utc_now()

                db.add(invoice)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        def _invoice_cached_pdf_is_fresh(invoice: Invoice) -> bool:
            """Return True when the persisted PDF is safe to reuse.

            In development we prefer always regenerating so style/template
            changes are visible immediately. In production we reuse the cached
            file only when it is not older than the current invoice row.
            """

            if str(settings.app_env or "").strip().lower() != "prod":
                return False
            pdf_path = str(getattr(invoice, "pdf_path", "") or "").strip()
            if not pdf_path:
                return False
            pdf_generated_at = getattr(invoice, "pdf_generated_at", None)
            if pdf_generated_at is None:
                return False
            pdf_template_updated_at = datetime(2026, 7, 6, 23, 30, 0)
            if pdf_generated_at < pdf_template_updated_at:
                return False
            updated_at = getattr(invoice, "updated_at", None)
            if updated_at is not None and pdf_generated_at < updated_at:
                return False
            return True

        def _regenerate_invoice_pdf_best_effort(
            request: Request,
            db: Session,
            *,
            invoice_id: int,
            subject_id: int,
        ) -> None:
            """Regenerate and persist invoice PDF after issue/edit.

            Never raises to the caller.
            """

            try:
                invoice_pdf_obj = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(subject_id))
                    .where(Invoice.contact.has(Contact.subject_id == int(subject_id)))
                    .options(selectinload(Invoice.contact))
                )
                if invoice_pdf_obj is None or str(invoice_pdf_obj.status or "") == "draft":
                    return

                items_pdf = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()

                ctx = _build_invoice_print_context(db, invoice=invoice_pdf_obj, items=list(items_pdf))

                pdf_bytes: bytes | None = None
                try:
                    html = templates.get_template("invoices/print.html").render(
                        {
                            "request": request,
                            **ctx,
                            "pdf_mode": True,
                            "app_css": _load_app_css(),
                        }
                    )
                    pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                except Exception:
                    pdf_bytes = None

                if pdf_bytes is None:
                    pdf_data = _invoice_pdf_data_from_context(invoice=invoice_pdf_obj, ctx=ctx)
                    try:
                        pdf_bytes = render_invoice_pdf_bytes(pdf_data)
                    except Exception:
                        pdf_bytes = None

                if pdf_bytes is not None:
                    _persist_invoice_pdf_best_effort(db, invoice=invoice_pdf_obj, pdf_bytes=pdf_bytes)
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        def _refresh_issued_invoice_snapshots_and_pdfs(
            request: Request,
            db: Session,
            *,
            subject_id: int,
            user_id: int | None = None,
        ) -> dict[str, int]:
            """Refresh seller/bank snapshots and persist PDFs for already issued invoices."""

            subject = db.get(Subject, int(subject_id))
            if subject is None:
                return {"invoice_count": 0, "pdf_count": 0}

            invoices = db.scalars(
                select(Invoice)
                .where(Invoice.subject_id == int(subject_id))
                .where(Invoice.status.in_(ISSUED_INVOICE_REFRESH_STATUSES))
                .where(Invoice.contact.has(Contact.subject_id == int(subject_id)))
                .where(_invoice_visible_in_lists_clause())
                .order_by(Invoice.issue_date.asc(), Invoice.id.asc())
            ).all()

            invoice_ids: list[int] = []
            for invoice_row in invoices:
                invoice_ids.append(int(invoice_row.id))
                _upsert_invoice_party(
                    db,
                    invoice_id=int(invoice_row.id),
                    role="seller",
                    payload=_party_payload_from_subject(subject),
                    sync_existing=True,
                )

                default_account = _default_subject_bank_account(
                    db,
                    subject_id=int(subject_id),
                    currency=str(getattr(invoice_row, "currency", "") or "CZK"),
                )
                if default_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        invoice_row,
                        account=default_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                elif str(getattr(subject, "bank_account", "") or "").strip():
                    invoice_row.bank_account_id = None
                    invoice_row.bank_account_number = None
                    invoice_row.bank_account_iban = None
                    invoice_row.bank_account_bic = None
                    invoice_row.bank_account_country = None
                    _apply_invoice_bank_account_snapshot(
                        invoice_row,
                        account=None,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                db.add(invoice_row)

            if invoice_ids:
                _audit_log(
                    db,
                    request=request,
                    action="invoice_bulk_pdf_refresh",
                    entity_type="subject",
                    entity_id=int(subject_id),
                    subject_id=int(subject_id),
                    user_id=user_id,
                    data={"invoice_ids": invoice_ids, "count": len(invoice_ids)},
                )
            db.commit()

            pdf_count = 0
            for invoice_id in invoice_ids:
                _regenerate_invoice_pdf_best_effort(
                    request,
                    db,
                    invoice_id=int(invoice_id),
                    subject_id=int(subject_id),
                )
                pdf_count += 1

            return {"invoice_count": len(invoice_ids), "pdf_count": int(pdf_count)}

        def _render_invoice_print(
            *,
            request: Request,
            db: Session,
            invoice_id: int,
            status_code: int = 200,
        ):
            """Print-friendly invoice page.

            Keep this HTML-first (no PDF dependency yet). It's usable as:
            - browser print (Ctrl+P)
            - "Save as PDF" in the browser
            """
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Tisk faktury", db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == invoice_id)
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Tisk faktury", db_error=str(exc))

            ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))
            subject = None
            try:
                subject = _load_subject_for_current_session(db)
            except Exception:
                subject = None
            public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
            ctx.update(
                {
                    "preview_mode": True,
                    "back_url": f"/invoices/{invoice.id}",
                    "send_email_url": f"/invoices/{invoice.id}#send-email",
                    "send_reminder_url": (
                        f"/invoices/{invoice.id}#send-reminder"
                        if (getattr(invoice, "due_date", None) and invoice.due_date < date.today() and str(invoice.status or "") != "paid")
                        else None
                    ),
                    "internal_pdf_url": f"/invoices/{invoice.id}/pdf",
                    "internal_pdf_download_url": f"/invoices/{invoice.id}/pdf?download=1",
                    "internal_isdoc_url": f"/invoices/{invoice.id}/isdoc",
                    "internal_isdoc_download_url": f"/invoices/{invoice.id}/isdoc?download=1",
                    "public_url": public_urls["view"] if public_urls else None,
                    "public_pdf_url": public_urls["pdf"] if public_urls else None,
                    "public_pdf_download_url": public_urls["pdf_download"] if public_urls else None,
                    "public_isdoc_url": public_urls["isdoc"] if public_urls else None,
                    "public_isdoc_download_url": public_urls["isdoc_download"] if public_urls else None,
                }
            )
            resp = templates.TemplateResponse(request, "invoices/print.html", ctx, status_code=status_code)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        def _invoice_pdf_error_response(
            request: Request,
            *,
            title: str,
            message: str,
            invoice_number: str | None = None,
        ):
            """Return a PDF (or a friendly HTML) for error situations."""

            try:
                pdf_bytes = render_error_pdf_bytes(
                    title=title,
                    message=message,
                    request_path=str(request.url.path),
                )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition_inline(invoice_number)},
                )
            except Exception as exc:  # pragma: no cover
                # If PDF rendering fails (missing deps), fall back to HTML.
                return _render_db_disabled(request, title=title, db_error=str(exc))

        def _invoice_isdoc_response(
            *,
            invoice: Invoice,
            ctx: dict,
            download: bool,
        ) -> Response:
            filename_base = safe_filename_base(str(invoice.number or f"invoice-{int(invoice.id)}"), fallback=f"invoice-{int(invoice.id)}")
            disposition = (
                content_disposition_attachment(filename_base, suffix=".isdoc")
                if bool(download)
                else content_disposition_inline(filename_base, suffix=".isdoc")
            )
            return Response(
                content=build_isdoc_bytes(invoice=invoice, ctx=ctx),
                media_type="application/xml; charset=utf-8",
                headers={
                    "Content-Disposition": disposition,
                    "Cache-Control": "no-store",
                },
            )

        @app.get("/invoices/{invoice_id}/pdf")
        def invoices_pdf(invoice_id: int, request: Request, download: bool = False, db: Session = Depends(get_db)):
            """Server-side PDF export of an invoice.

            Unlike the HTML print page, this endpoint returns "application/pdf".
            If DB is unavailable, we still return a small PDF with an error message.
            """

            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst fakturu.",
                )

            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            # Phase-20: if an issued PDF is already persisted on disk, serve it.
            if str(invoice.status or "").strip().lower() != "draft" and _invoice_cached_pdf_is_fresh(invoice):
                cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
                if cached is not None and bytes(cached).startswith(b"%PDF"):
                    disp = (
                        content_disposition_attachment(invoice.number)
                        if bool(download)
                        else content_disposition_inline(invoice.number)
                    )
                    return Response(
                        content=cached,
                        media_type="application/pdf",
                        headers={
                            "Content-Disposition": disp,
                            "Cache-Control": "no-store",
                        },
                    )

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == invoice_id)
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst položky faktury.",
                    invoice_number=invoice.number,
                )

            ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))
            disp = (
                content_disposition_attachment(invoice.number)
                if bool(download)
                else content_disposition_inline(invoice.number)
            )

            # ----------------------------------------------------------
            # Phase-19: HTML → PDF via WeasyPrint (preview)
            # ----------------------------------------------------------
            try:
                html = templates.get_template("invoices/print.html").render(
                    {
                        "request": request,
                        **ctx,
                        "pdf_mode": True,
                        "app_css": _load_app_css(),
                    }
                )
                pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
                )
            except Exception:
                # If WeasyPrint fails (missing native deps, incompatible versions,
                # etc.), fall back to ReportLab.
                pass

            pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)

            try:
                pdf_bytes = render_invoice_pdf_bytes(pdf_data)
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

            _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
            )

        @app.get("/invoices/{invoice_id}/isdoc")
        def invoices_isdoc(invoice_id: int, request: Request, download: bool = False, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))

            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == invoice_id)
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))

            ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))
            return _invoice_isdoc_response(invoice=invoice, ctx=ctx, download=download)

        # ------------------------------------------------------------------
        # Phase-21: public invoice URL (token) + rate limit + PDF download
        # ------------------------------------------------------------------
        def _public_invoice_urls_for_subject_and_invoice(
            request: Request,
            *,
            subject: Subject,
            invoice: Invoice,
        ) -> dict[str, str]:
            public_base_url = resolve_public_base_url(
                request=request,
                configured_base_url=getattr(settings, "public_base_url", "") or None,
                trusted_proxy_ips=getattr(settings, "trusted_proxy_ips", ()) or (),
                allow_host_header_fallback=not (settings.app_env == "prod" and settings.auth_required),
            )
            return build_public_invoice_urls(
                base_url=public_base_url,
                public_username=(getattr(subject, "public_username", "") or "").strip().lower(),
                token=str(getattr(invoice, "public_token", "") or ""),
                invoice_number=str(getattr(invoice, "number", "") or ""),
                invoice_id=int(getattr(invoice, "id", 0) or 0),
                secret_key=str(settings.public_link_hmac_key or ""),
            )

        def _public_invoice_relative_urls_for_subject_and_invoice(
            *,
            subject: Subject,
            invoice: Invoice,
        ) -> dict[str, str] | None:
            public_username = (getattr(subject, "public_username", "") or "").strip().lower()
            token = (getattr(invoice, "public_token", "") or "").strip()
            if str(getattr(invoice, "status", "") or "").strip().lower() == "draft":
                return None
            if not public_username or not token:
                return None
            return build_public_invoice_urls(
                base_url=None,
                public_username=public_username,
                token=token,
                invoice_number=str(getattr(invoice, "number", "") or ""),
                invoice_id=int(getattr(invoice, "id", 0) or 0),
                secret_key=str(settings.public_link_hmac_key or ""),
            )

        def _load_public_subject_and_invoice_by_legacy_token(
            db: Session,
            *,
            public_username: str,
            token: str,
        ) -> tuple[Subject | None, Invoice | None]:
            pu = (public_username or "").strip().lower()
            if not _PUBLIC_USERNAME_RE.match(pu):
                return None, None

            subject = db.scalar(select(Subject).where(Subject.public_username == pu))
            if subject is None:
                return None, None

            invoice = db.scalar(
                select(Invoice)
                .where(Invoice.subject_id == int(subject.id))
                .where(Invoice.public_token == str(token))
                .options(selectinload(Invoice.contact))
            )
            return subject, invoice

        def _load_public_subject_and_invoice_by_short_code(
            db: Session,
            *,
            short_code: str,
        ) -> tuple[Subject | None, Invoice | None]:
            parsed = parse_public_invoice_short_code(short_code)
            if parsed is None:
                return None, None
            invoice_id, _sig = parsed

            invoice = db.scalar(
                select(Invoice)
                .where(Invoice.id == int(invoice_id))
                .options(selectinload(Invoice.contact))
            )
            if invoice is None:
                return None, None

            token = (getattr(invoice, "public_token", None) or "").strip()
            if not token:
                return None, None
            if not verify_public_invoice_short_code(
                short_code=short_code,
                invoice_id=int(invoice.id),
                token=token,
                secret_key=str(settings.public_link_hmac_key or ""),
            ):
                return None, None

            subject = db.scalar(select(Subject).where(Subject.id == int(invoice.subject_id)).limit(1))
            if subject is None:
                return None, None
            return subject, invoice





        def _mark_invoice_public_access_best_effort(*, invoice_id: int) -> None:
            try:
                from fakturek.db import get_sessionmaker  # type: ignore
                from sqlalchemy import func as sa_func, update as sa_update  # type: ignore

                SessionLocal = get_sessionmaker()
                with SessionLocal() as tracking_db:  # type: ignore
                    now = utc_now()
                    first_viewed_at = tracking_db.scalar(
                        select(Invoice.public_first_viewed_at).where(Invoice.id == int(invoice_id))
                    )
                    values: dict[str, object] = {
                        "public_view_count": sa_func.coalesce(Invoice.public_view_count, 0) + 1,
                        "public_last_viewed_at": now,
                    }
                    if first_viewed_at is None:
                        values["public_first_viewed_at"] = now
                    tracking_db.execute(
                        sa_update(Invoice)
                        .where(Invoice.id == int(invoice_id))
                        .values(**values)
                    )
                    tracking_db.commit()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to track public invoice access for invoice_id=%s",
                    invoice_id,
                )

        def _should_track_public_invoice_access(
            request: Request,
            *,
            db: Session,
            subject_id: int | None,
        ) -> bool:
            try:
                target_subject_id = int(subject_id or 0)
            except Exception:
                return True
            if target_subject_id <= 0:
                return True
            user_id = _current_user_id_or_none(request)
            if user_id is None:
                return True
            try:
                return not _user_can_view_subject(db, user_id=int(user_id), subject_id=target_subject_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to resolve public invoice tracking permissions for user_id=%s subject_id=%s",
                    user_id,
                    target_subject_id,
                )
                return True

        @app.get("/{public_username}/i/{token}/{invoice_number}", response_class=HTMLResponse)
        def public_invoice_view(
            public_username: str,
            token: str,
            invoice_number: str,
            request: Request,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request)
            try:
                subject, invoice = _load_public_subject_and_invoice_by_legacy_token(
                    db,
                    public_username=public_username,
                    token=token,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Veřejná faktura", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_no = str(invoice.number or "")
            if canonical_no and str(invoice_number or "") != canonical_no:
                inv_no = quote(canonical_no, safe="")
                return RedirectResponse(url=f"/{public_username}/i/{token}/{inv_no}", status_code=307)

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Veřejná faktura", db_error=str(exc))

            ctx = _build_invoice_print_context(
                db,
                invoice=invoice,
                items=list(items),
                subject_override=subject,
            )

            public_urls = _public_invoice_urls_for_subject_and_invoice(
                request,
                subject=subject,
                invoice=invoice,
            )
            ctx.update(
                {
                    "public_mode": True,
                    "public_url": public_urls["view"],
                    "public_pdf_url": public_urls["pdf"],
                    "public_pdf_download_url": public_urls["pdf_download"],
                    "public_isdoc_url": public_urls["isdoc"],
                    "public_isdoc_download_url": public_urls["isdoc_download"],
                    "public_invoice_payment": {},
                    "back_url": _public_back_url_for_invoice(request, db=db, invoice=invoice),
                }
            )

            resp = templates.TemplateResponse(request, "invoices/print.html", ctx)
            resp.headers["Cache-Control"] = "no-store"
            return resp

        @app.get("/{public_username}/i/{token}/{invoice_number}/pdf")
        def public_invoice_pdf(
            public_username: str,
            token: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="pdf")
            try:
                subject, invoice = _load_public_subject_and_invoice_by_legacy_token(
                    db,
                    public_username=public_username,
                    token=token,
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst fakturu.",
                )
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_no = str(invoice.number or "")
            if canonical_no and str(invoice_number or "") != canonical_no:
                inv_no = quote(canonical_no, safe="")
                return RedirectResponse(url=f"/{public_username}/i/{token}/{inv_no}/pdf", status_code=307)

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            # If a persisted issued PDF exists, return it directly.
            if str(invoice.status or "").strip().lower() != "draft" and _invoice_cached_pdf_is_fresh(invoice):
                cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
                if cached is not None and bytes(cached).startswith(b"%PDF"):
                    disp = (
                        content_disposition_attachment(invoice.number)
                        if bool(download)
                        else content_disposition_inline(invoice.number)
                    )
                    return Response(
                        content=cached,
                        media_type="application/pdf",
                        headers={
                            "Content-Disposition": disp,
                            "Cache-Control": "no-store",
                        },
                    )

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst položky faktury.",
                    invoice_number=invoice.number,
                )

            ctx = _build_invoice_print_context(
                db,
                invoice=invoice,
                items=list(items),
                subject_override=subject,
            )

            disp = (
                content_disposition_attachment(invoice.number)
                if bool(download)
                else content_disposition_inline(invoice.number)
            )

            # Prefer HTML → PDF via WeasyPrint.
            try:
                html = templates.get_template("invoices/print.html").render(
                    {
                        "request": request,
                        **ctx,
                        "pdf_mode": True,
                        "public_mode": True,
                        "app_css": _load_app_css(),
                    }
                )
                pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
                )
            except Exception:
                pass

            pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)

            try:
                pdf_bytes = render_invoice_pdf_bytes(pdf_data)
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

            _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
            )

        @app.get("/{public_username}/i/{token}/{invoice_number}/isdoc")
        def public_invoice_isdoc(
            public_username: str,
            token: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="isdoc")
            try:
                subject, invoice = _load_public_subject_and_invoice_by_legacy_token(
                    db,
                    public_username=public_username,
                    token=token,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_no = str(invoice.number or "")
            if canonical_no and str(invoice_number or "") != canonical_no:
                inv_no = quote(canonical_no, safe="")
                return RedirectResponse(url=f"/{public_username}/i/{token}/{inv_no}/isdoc", status_code=307)

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))

            ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items), subject_override=subject)
            return _invoice_isdoc_response(invoice=invoice, ctx=ctx, download=download)

        @app.get("/i/{short_code}/{invoice_number}/pdf")
        def public_invoice_pdf_short_readable(
            short_code: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="pdf")

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst fakturu.",
                )
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_hint = slugify_public_invoice_number(str(invoice.number or ""))
            if canonical_hint and str(invoice_number or "").strip().lower() != canonical_hint:
                redirect_url = (
                    relative_public_urls["pdf_download"]
                    if bool(download)
                    else relative_public_urls["pdf"]
                )
                return RedirectResponse(url=redirect_url, status_code=307)

            if str(invoice.status or "").strip().lower() != "draft" and _invoice_cached_pdf_is_fresh(invoice):
                cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
                if cached is not None and bytes(cached).startswith(b"%PDF"):
                    disp = (
                        content_disposition_attachment(invoice.number)
                        if bool(download)
                        else content_disposition_inline(invoice.number)
                    )
                    return Response(
                        content=cached,
                        media_type="application/pdf",
                        headers={
                            "Content-Disposition": disp,
                            "Cache-Control": "no-store",
                        },
                    )

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst položky faktury.",
                    invoice_number=invoice.number,
                )

            ctx = _build_invoice_print_context(
                db,
                invoice=invoice,
                items=list(items),
                subject_override=subject,
            )

            disp = (
                content_disposition_attachment(invoice.number)
                if bool(download)
                else content_disposition_inline(invoice.number)
            )

            try:
                html = templates.get_template("invoices/print.html").render(
                    {
                        "request": request,
                        **ctx,
                        "pdf_mode": True,
                        "public_mode": True,
                        "app_css": _load_app_css(),
                    }
                )
                pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
                )
            except Exception:
                pass

            pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)

            try:
                pdf_bytes = render_invoice_pdf_bytes(pdf_data)
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

            _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=pdf_bytes)

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
            )

        @app.get("/i/{short_code}/{invoice_number}/isdoc")
        def public_invoice_isdoc_short_readable(
            short_code: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="isdoc")

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_hint = slugify_public_invoice_number(str(invoice.number or ""))
            if canonical_hint and str(invoice_number or "").strip().lower() != canonical_hint:
                redirect_url = (
                    relative_public_urls["isdoc_download"]
                    if bool(download)
                    else relative_public_urls["isdoc"]
                )
                return RedirectResponse(url=redirect_url, status_code=307)

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))

            ctx = _build_invoice_print_context(
                db,
                invoice=invoice,
                items=list(items),
                subject_override=subject,
            )
            return _invoice_isdoc_response(invoice=invoice, ctx=ctx, download=download)

        @app.get("/i/{short_code}/pdf")
        def public_invoice_pdf_short(
            short_code: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="pdf")

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF faktury",
                    message="Databáze není dostupná – nelze načíst fakturu.",
                )
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            redirect_url = relative_public_urls["pdf_download"] if bool(download) else relative_public_urls["pdf"]
            return RedirectResponse(url=redirect_url, status_code=307)

        @app.get("/i/{short_code}/isdoc")
        def public_invoice_isdoc_short(
            short_code: str,
            request: Request,
            download: bool = False,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request, key_suffix="isdoc")

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="ISDOC faktury", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            redirect_url = relative_public_urls["isdoc_download"] if bool(download) else relative_public_urls["isdoc"]
            return RedirectResponse(url=redirect_url, status_code=307)

        @app.get("/i/{short_code}/{invoice_number}", response_class=HTMLResponse)
        def public_invoice_view_short_readable(
            short_code: str,
            invoice_number: str,
            request: Request,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request)

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Veřejná faktura", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            canonical_hint = slugify_public_invoice_number(str(invoice.number or ""))
            if canonical_hint and str(invoice_number or "").strip().lower() != canonical_hint:
                return RedirectResponse(url=relative_public_urls["view"], status_code=307)

            if _should_track_public_invoice_access(request, db=db, subject_id=getattr(invoice, "subject_id", None)):
                _mark_invoice_public_access_best_effort(invoice_id=int(invoice.id))

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice.id))
                    .order_by(InvoiceItem.sort_order.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Veřejná faktura", db_error=str(exc))

            ctx = _build_invoice_print_context(
                db,
                invoice=invoice,
                items=list(items),
                subject_override=subject,
            )
            public_urls = _public_invoice_urls_for_subject_and_invoice(
                request,
                subject=subject,
                invoice=invoice,
            )
            ctx.update(
                {
                    "public_mode": True,
                    "public_url": public_urls["view"],
                    "public_pdf_url": public_urls["pdf"],
                    "public_pdf_download_url": public_urls["pdf_download"],
                    "public_isdoc_url": public_urls["isdoc"],
                    "public_isdoc_download_url": public_urls["isdoc_download"],
                    "public_invoice_payment": {},
                    "back_url": _public_back_url_for_invoice(request, db=db, invoice=invoice),
                }
            )

            resp = templates.TemplateResponse(request, "invoices/print.html", ctx)
            resp.headers["Cache-Control"] = "no-store"
            return resp

        @app.get("/i/{short_code}", response_class=HTMLResponse)
        def public_invoice_view_short(
            short_code: str,
            request: Request,
            db: Session = Depends(get_db),
        ):
            _public_rate_limit_or_429(request)

            try:
                subject, invoice = _load_public_subject_and_invoice_by_short_code(
                    db,
                    short_code=short_code,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Veřejná faktura", db_error=str(exc))
            if subject is None or invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            relative_public_urls = _public_invoice_relative_urls_for_subject_and_invoice(
                subject=subject,
                invoice=invoice,
            )
            if relative_public_urls is None:
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            return RedirectResponse(url=relative_public_urls["view"], status_code=307)

        @app.get("/invoices", response_class=HTMLResponse)
        def invoices_list(
            request: Request,
            db: Session = Depends(get_db),
            q: str | None = None,
            status: str | None = None,
            contact_id: str | None = None,
            document_type: str | None = None,
            overdue: bool = False,
            credit_note_mode: bool = False,
            page: int = 1,
            notice: str | None = None,
            error: str | None = None,
        ):
            sid = _current_subject_id()
            today = date.today()

            # Used in templates for post/redirect/get actions.
            current_url = request.url.path
            if request.url.query:
                current_url = f"{current_url}?{request.url.query}"

            try:
                contacts = db.scalars(
                    select(Contact)
                    .where(Contact.subject_id == sid)
                    .order_by(Contact.name.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Faktury", db_error=str(exc))

            q_clean = (q or "").strip()
            status_clean = (status or "").strip()
            document_type_clean = _invoice_document_type_filter_value(document_type)
            contact_id_clean = str(contact_id or "").strip()
            cid = int(contact_id_clean) if contact_id_clean.isdigit() and int(contact_id_clean) > 0 else None
            filters = [
                Invoice.subject_id == sid,
                Invoice.contact.has(Contact.subject_id == sid),
                _invoice_visible_in_lists_clause(),
            ]

            if status_clean and status_clean in ALLOWED_INVOICE_STATUSES:
                filters.append(Invoice.status == status_clean)
            else:
                status_clean = ""

            if document_type_clean:
                filters.append(Invoice.document_type == document_type_clean)

            if cid:
                filters.append(Invoice.contact_id == int(cid))

            if q_clean:
                like = f"%{q_clean}%"
                filters.append(
                    or_(
                        Invoice.number.like(like),
                        Invoice.contact.has(Contact.name.like(like)),
                    )
                )

            if overdue:
                filters.append(Invoice.due_date < today)
                filters.append(Invoice.status != "paid")

            pagination = _build_pagination_payload(
                request,
                page=_normalize_page_number(page),
                per_page=50,
                total_count=int(db.scalar(select(func.count(Invoice.id)).where(*filters)) or 0),
            )

            try:
                invoices = db.scalars(
                    select(Invoice)
                    .where(*filters)
                    .options(selectinload(Invoice.contact))
                    .order_by(*_invoice_newest_first_ordering())
                    .offset(int(pagination["offset"]))
                    .limit(int(pagination["limit"]))
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Faktury", db_error=str(exc))

            try:
                draft_invoice_count = int(
                    db.scalar(
                        select(func.count(Invoice.id)).where(
                            Invoice.subject_id == sid,
                            Invoice.contact.has(Contact.subject_id == sid),
                            _invoice_visible_in_lists_clause(),
                            Invoice.status == "draft",
                        )
                    )
                    or 0
                )
            except SQLAlchemyError:
                draft_invoice_count = 0

            # Phase-23: expose SMTP readiness for quick reminder action in lists.
            subject = _load_subject_for_current_session(db)
            mail_ctx = _mail_identity_context(db, subject=subject, request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "").strip()

            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )

            smtp_ready = smtp_is_configured(smtp_cfg) and looks_like_email(from_email)
            recurring_plans = db.scalars(
                select(RecurringInvoicePlan)
                .options(selectinload(RecurringInvoicePlan.template_invoice))
                .where(RecurringInvoicePlan.subject_id == int(sid))
                .order_by(RecurringInvoicePlan.is_active.desc(), RecurringInvoicePlan.next_issue_date.asc(), RecurringInvoicePlan.id.asc())
            ).all()

            return templates.TemplateResponse(
                request,
                "invoices/list.html",
                {
                    "invoices": invoices,
                    "contacts": contacts,
                    "filters": {
                        "q": q_clean,
                        "status": status_clean,
                        "contact_id": cid,
                        "document_type": document_type_clean,
                        "overdue": bool(overdue),
                    },
                    "pagination": pagination,
                    "notice": notice,
                    "error": error,
                    "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                    "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                    "allowed_statuses": ALLOWED_INVOICE_STATUSES,
                    "document_type_options": _invoice_list_filter_options(),
                    "today": today,
                    "next_url": current_url,
                    "smtp_ready": bool(smtp_ready),
                    "credit_note_mode": bool(credit_note_mode),
                    "draft_invoice_count": draft_invoice_count,
                    "bulk_action_options": BULK_INVOICE_ACTION_OPTIONS,
                    "recurring_plans": [_recurring_plan_summary(plan) for plan in recurring_plans],
                    "recurring_interval_options": RECURRING_INTERVAL_OPTIONS,
                },
            )

        @app.post("/settings/invoices/regenerate-pdfs")
        @app.post("/invoices/regenerate-issued-pdfs")
        async def invoices_regenerate_issued_pdfs(request: Request, db: Session = Depends(get_db)):
            await _verify_csrf(request)
            form = await _request_form_once(request)
            sid = _current_subject_id()
            user_id = _current_user_id_or_none(request)
            next_url = _safe_next_url(str(form.get("next") or ""), "/settings#issuer")

            if settings.auth_required and user_id is None:
                return RedirectResponse(url=f"/login?next={quote(next_url, safe='')}", status_code=303)

            can_refresh = True
            if settings.auth_required:
                link = _user_subject_link(db, user_id=user_id, subject_id=int(sid))
                can_refresh = (
                    _user_can_manage_subject(db, user_id=user_id, subject_id=int(sid))
                    or bool(getattr(link, "can_edit", False))
                    or bool(getattr(link, "can_issue", False))
                )
            if not can_refresh:
                raise HTTPException(status_code=403, detail="Access denied")

            try:
                result = _refresh_issued_invoice_snapshots_and_pdfs(
                    request,
                    db,
                    subject_id=int(sid),
                    user_id=user_id,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                try:
                    db.rollback()
                except Exception:
                    pass
                return _render_db_disabled(request, title="Přegenerování PDF", db_error=str(exc), status_code=500)
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                result = {"invoice_count": 0, "pdf_count": 0}

            invoice_count = int(result.get("invoice_count") or 0)
            if invoice_count <= 0:
                message = "Nenašel jsem žádné vystavené faktury k přegenerování."
            elif invoice_count == 1:
                message = "Přegenerováno 1 vystavené PDF z aktuálních fakturačních údajů."
            else:
                message = f"Přegenerováno {invoice_count} vystavených PDF z aktuálních fakturačních údajů."
            param = "info" if next_url.startswith("/settings") else "notice"
            return RedirectResponse(url=_with_query_params(next_url, **{param: message}), status_code=303)

        @app.get("/invoices/export.csv")
        def invoices_export_csv(
            request: Request,
            db: Session = Depends(get_db),
            q: str | None = None,
            status: str | None = None,
            contact_id: int | None = None,
            document_type: str | None = None,
            overdue: bool = False,
        ):
            sid = _current_subject_id()
            try:
                subject = _load_subject_for_current_session(db)
                subject_slug = _export_subject_slug(subject, subject_id=int(sid))
                rows = _export_invoices_rows(
                    db,
                    subject_id=int(sid),
                    q=q,
                    status=status,
                    contact_id=contact_id,
                    document_type=document_type,
                    overdue=bool(overdue),
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Faktury", db_error=str(exc), status_code=500)
            return _csv_attachment_response(
                [
                    "id",
                    "number",
                    "document_type",
                    "source_invoice_id",
                    "source_invoice_number",
                    "status",
                    "issue_date",
                    "taxable_supply_date",
                    "due_date",
                    "paid_on",
                    "currency",
                    "items_total",
                    "items_total_cents",
                    "discount",
                    "discount_cents",
                    "subtotal_after_discount",
                    "subtotal_after_discount_cents",
                    "total",
                    "total_cents",
                    "rounding_adjustment",
                    "rounding_adjustment_cents",
                    "contact_id",
                    "contact_name",
                    "contact_email",
                    "contact_ico",
                    "series_name",
                    "bank_account_label",
                    "bank_account_number",
                    "bank_account_iban",
                    "bank_account_bic",
                    "bank_account_country",
                    "notes",
                    "internal_notes",
                    "issued_at",
                    "sent_at",
                    "reminder_sent_at",
                    "public_url_enabled",
                    "pdf_generated_at",
                    "created_at",
                    "updated_at",
                ],
                rows,
                filename=f"{subject_slug}-invoices.csv",
            )

        @app.get("/invoices/new", response_class=HTMLResponse)
        def invoices_new(
            request: Request,
            db: Session = Depends(get_db),
            contact_id: int | None = None,
            document_type: str | None = None,
            recurring_mode: bool = False,
        ):
            normalized_document_type = _normalize_invoice_document_type(document_type)
            try:
                sid = _current_subject_id()
                contacts = db.scalars(
                    select(Contact)
                    .where(Contact.subject_id == sid)
                    .order_by(Contact.name.asc())
                ).all()

                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=normalized_document_type,
                )
                series_list = db.scalars(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.subject_id == sid)
                    .order_by(InvoiceSeries.name.asc())
                ).all()
                bank_accounts_rows = _list_subject_bank_accounts(db, subject_id=sid)
                subject = _load_subject_for_current_session(db)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))

            today = date.today()
            is_vat_payer, default_currency = _subject_flags(db)
            subject_country = _invoice_subject_country(subject)
            vat_rate_options = _invoice_vat_rate_options(subject_country)
            default_vat_rate = _invoice_default_vat_rate(subject_country)
            _sync_series_list_for_year(
                db,
                subject_id=sid,
                series_list=series_list,
                year=today.year,
            )
            default_bank_account = _default_subject_bank_account(db, subject_id=sid, currency=default_currency)
            default_footer_mode = _default_invoice_footer_mode(subject)
            default_invoice_style = _default_invoice_style(subject)
            recurring_prefill = _build_recurring_prefill(today)
            selected_contact = next((c for c in contacts if int(getattr(c, "id", 0) or 0) == int(contact_id)) , None) if contact_id else None
            seller_party_prefill = _normalize_party_payload(_party_payload_from_subject(subject))
            buyer_party_prefill = _normalize_party_payload(_party_payload_from_contact(selected_contact))
            prefill = {
                "contact_id": contact_id or None,
                "document_type": normalized_document_type,
                "source_invoice_id": None,
                "issue_date": today.isoformat(),
                "taxable_supply_date": today.isoformat(),
                "due_date": (today + timedelta(days=14)).isoformat(),
                "currency": default_currency,
                "invoice_language": "cs",
                "invoice_style": default_invoice_style,
                "series_id": int(default_series.id) if default_series else None,
                "bank_account_id": int(default_bank_account.id) if default_bank_account is not None else None,
                "payment_method": "bank_transfer",
                "footer_mode": default_footer_mode,
                "footer_text": _invoice_footer_text_for_mode(default_footer_mode, subject=subject),
                "due_term": "14",
                "discount_amount": "",
                "rounding_enabled": False,
                "rounding_adjustment": "",
                "notes": "",
                "variable_symbol": _contact_fixed_variable_symbol(selected_contact) if selected_contact is not None else "",
                "final_number_preview": _series_next_number_preview(default_series, year=today.year),
                "seller_party": seller_party_prefill,
                "buyer_party": buyer_party_prefill,
            }
            prefill, prefill_items = _apply_invoice_editor_summary(
                prefill=prefill,
                prefill_items=[],
                is_vat_payer=bool(is_vat_payer),
                allow_negative_unit_price=normalized_document_type == "credit_note",
                min_rows=1,
                default_vat_rate=default_vat_rate,
            )

            return templates.TemplateResponse(
                request,
                "invoices/new.html",
                {
                    "contacts": contacts,
                    "prefill": prefill,
                    "prefill_items": prefill_items,
                    "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                    "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                    "recurring_mode": bool(recurring_mode),
                    "recurring_prefill": recurring_prefill,
                    "recurring_interval_options": RECURRING_INTERVAL_OPTIONS,
                    "series_options": _build_invoice_series_options(series_list, year=today.year),
                    "account_options": _build_bank_account_options(bank_accounts_rows),
                    "currency_options": _build_currency_options(prefill.get("currency")),
                    "catalog_items": _list_invoice_catalog_items(db, subject_id=int(sid), currency=prefill.get("currency"), limit=12),
                    "is_vat_payer": bool(is_vat_payer),
                    "vat_rate_options": vat_rate_options,
                    "default_vat_rate": default_vat_rate,
                    "subject_country": subject_country,
                    "due_term_options": INVOICE_DUE_TERM_OPTIONS,
                    "item_unit_options": INVOICE_ITEM_UNIT_OPTIONS,
                    "payment_method_options": INVOICE_PAYMENT_METHOD_OPTIONS,
                    "invoice_language_options": INVOICE_LANGUAGE_OPTIONS,
                    "invoice_style_options": INVOICE_STYLE_OPTIONS,
                    "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                    "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                    "page_title": "Nová automatická faktura" if recurring_mode else _invoice_page_title_for_type(normalized_document_type, mode="new"),
                    "page_subtitle": (
                        "Vytvoř interní šablonu a rovnou nastav opakování. Do běžného seznamu faktur se ta šablona plést nebude."
                        if recurring_mode
                        else _new_invoice_page_subtitle(normalized_document_type)
                    ),
                    "submit_label": "Vytvořit automatickou fakturu" if recurring_mode else _new_invoice_submit_label(normalized_document_type),
                    "draft_submit_label": "Uložit koncept" if not recurring_mode else "",
                    "show_draft_submit": not bool(recurring_mode),
                    "back_url": "/recurring" if recurring_mode else "/invoices",
                },
            )

        @app.post("/invoices/new")
        async def invoices_create(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            normalized_document_type = _normalize_invoice_document_type(form.get("document_type"))
            recurring_mode = str(form.get("recurring_mode") or "").strip().lower() in {"1", "true", "on", "yes"}
            submit_action = str(form.get("submit_action") or "").strip().lower()

            try:
                sid = _current_subject_id()
                contacts = db.scalars(
                    select(Contact)
                    .where(Contact.subject_id == sid)
                    .order_by(Contact.name.asc())
                ).all()

                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=normalized_document_type,
                )
                series_list = db.scalars(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.subject_id == sid)
                    .order_by(InvoiceSeries.name.asc())
                ).all()
                bank_accounts_rows = _list_subject_bank_accounts(db, subject_id=sid)
                subject = _load_subject_for_current_session(db)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))
            today = date.today()

            is_vat_payer, default_currency = _subject_flags(db)
            subject_country = _invoice_subject_country(subject)
            vat_rate_options = _invoice_vat_rate_options(subject_country)
            default_vat_rate = _invoice_default_vat_rate(subject_country)
            default_bank_account = _default_subject_bank_account(
                db,
                subject_id=sid,
                currency=(form.get("currency") or default_currency),
            )
            recurring_prefill = _prefill_recurring_from_form(form)

            def _preview_year(value: str | None) -> int:
                try:
                    return date.fromisoformat(str(value or "")).year
                except Exception:
                    return today.year

            def _prefill_from_form() -> dict:
                try:
                    cid = int(form.get("contact_id")) if form.get("contact_id") else None
                except Exception:
                    cid = None
                try:
                    series_id = int(form.get("series_id")) if form.get("series_id") else None
                except Exception:
                    series_id = None

                bank_account_raw = None
                try:
                    if "bank_account_id" in form:
                        bank_account_raw = (form.get("bank_account_id") or "").strip()
                    elif default_bank_account is not None:
                        bank_account_raw = str(int(default_bank_account.id))
                    else:
                        bank_account_raw = ""
                except Exception:
                    bank_account_raw = ""

                return {
                    "contact_id": cid,
                    "document_type": normalized_document_type,
                    "source_invoice_id": (
                        int(form.get("source_invoice_id"))
                        if str(form.get("source_invoice_id") or "").strip().isdigit()
                        else None
                    ),
                    "issue_date": (form.get("issue_date") or today.isoformat()).strip(),
                    "taxable_supply_date": (form.get("taxable_supply_date") or form.get("issue_date") or today.isoformat()).strip(),
                    "due_date": (
                        form.get("due_date")
                        or (today + timedelta(days=14)).isoformat()
                    ).strip(),
                    "currency": (form.get("currency") or default_currency).strip() or default_currency,
                    "invoice_language": _normalize_invoice_language(form.get("invoice_language")),
                    "invoice_style": _normalize_invoice_style(form.get("invoice_style") or _default_invoice_style(subject)),
                    "series_id": series_id,
                    "bank_account_id": bank_account_raw,
                    "payment_method": (form.get("payment_method") or "bank_transfer").strip() or "bank_transfer",
                    "footer_mode": (form.get("footer_mode") or _default_invoice_footer_mode(subject)).strip() or _default_invoice_footer_mode(subject),
                    "footer_text": (form.get("footer_text") or "").strip(),
                    "due_term": (form.get("due_term") or "14").strip() or "14",
                    "discount_amount": (form.get("discount_amount") or "").strip(),
                    "rounding_enabled": bool(form.get("rounding_enabled")),
                    "rounding_adjustment": (form.get("rounding_adjustment") or "").strip(),
                    "notes": (form.get("notes") or "").strip(),
                    "variable_symbol": _normalize_variable_symbol(form.get("variable_symbol")),
                    "seller_party": _party_payload_from_form(
                        form,
                        prefix="seller",
                        fallback=_party_payload_from_subject(subject),
                    ),
                    "buyer_party": _party_payload_from_form(
                        form,
                        prefix="buyer",
                        fallback=_party_payload_from_contact(None),
                    ),
                }

            prefill = _prefill_from_form()
            preview_year = _preview_year(prefill.get("issue_date"))
            _sync_series_list_for_year(
                db,
                subject_id=sid,
                series_list=series_list,
                year=preview_year,
            )
            selected_series_for_prefill = _pick_invoice_series_for_preview(
                series_list,
                selected_id=prefill.get("series_id"),
                default_series=default_series,
            )
            prefill["final_number_preview"] = _series_next_number_preview(selected_series_for_prefill, year=preview_year)
            raw_prefill_items: list[dict[str, str]] = []

            def _render_new_editor(*, error: str, status_code: int = 400):
                editor_prefill = dict(prefill)
                year = _preview_year(editor_prefill.get("issue_date"))
                editor_prefill["final_number_preview"] = _series_next_number_preview(
                    _pick_invoice_series_for_preview(
                        series_list,
                        selected_id=editor_prefill.get("series_id"),
                        default_series=default_series,
                    ),
                    year=year,
                )
                editor_prefill, editor_items = _apply_invoice_editor_summary(
                    prefill=editor_prefill,
                    prefill_items=raw_prefill_items,
                    is_vat_payer=bool(is_vat_payer),
                    allow_negative_unit_price=normalized_document_type == "credit_note",
                    min_rows=max(1, len(raw_prefill_items) or 1),
                    default_vat_rate=default_vat_rate,
                )
                return templates.TemplateResponse(
                    request,
                    "invoices/new.html",
                    {
                        "contacts": contacts,
                        "prefill": editor_prefill,
                        "prefill_items": editor_items,
                        "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                        "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                        "recurring_mode": bool(recurring_mode),
                        "recurring_prefill": recurring_prefill,
                        "recurring_interval_options": RECURRING_INTERVAL_OPTIONS,
                        "series_options": _build_invoice_series_options(series_list, year=year),
                        "account_options": _build_bank_account_options(bank_accounts_rows),
                        "currency_options": _build_currency_options(editor_prefill.get("currency")),
                        "catalog_items": _list_invoice_catalog_items(db, subject_id=int(sid), currency=editor_prefill.get("currency"), limit=12),
                        "is_vat_payer": bool(is_vat_payer),
                        "vat_rate_options": vat_rate_options,
                        "default_vat_rate": default_vat_rate,
                        "subject_country": subject_country,
                        "due_term_options": INVOICE_DUE_TERM_OPTIONS,
                        "item_unit_options": INVOICE_ITEM_UNIT_OPTIONS,
                        "payment_method_options": INVOICE_PAYMENT_METHOD_OPTIONS,
                        "invoice_language_options": INVOICE_LANGUAGE_OPTIONS,
                        "invoice_style_options": INVOICE_STYLE_OPTIONS,
                        "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                        "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                        "page_title": "Nová automatická faktura" if recurring_mode else _invoice_page_title_for_type(normalized_document_type, mode="new"),
                        "page_subtitle": (
                            "Vytvoř interní šablonu a rovnou nastav opakování. Do běžného seznamu faktur se ta šablona plést nebude."
                            if recurring_mode
                            else _new_invoice_page_subtitle(normalized_document_type)
                        ),
                        "submit_label": "Vytvořit automatickou fakturu" if recurring_mode else _new_invoice_submit_label(normalized_document_type),
                        "draft_submit_label": "Uložit koncept" if not recurring_mode else "",
                        "show_draft_submit": not bool(recurring_mode),
                        "back_url": "/recurring" if recurring_mode else "/invoices",
                        "error": error,
                    },
                    status_code=status_code,
                )

            try:
                items_payload, raw_prefill_items = _parse_invoice_items_from_form(
                    form,
                    is_vat_payer=bool(is_vat_payer),
                    allow_negative_unit_price=normalized_document_type == "credit_note",
                    default_vat_rate=default_vat_rate,
                )
            except ValueError as exc:
                return _render_new_editor(error=str(exc))

            if not prefill.get("series_id") and default_series is not None:
                prefill["series_id"] = int(default_series.id)

            if not prefill["contact_id"]:
                return _render_new_editor(error="Vyber odběratele.")

            try:
                issue_date = date.fromisoformat(prefill["issue_date"])
                taxable_supply_date = date.fromisoformat(prefill.get("taxable_supply_date") or prefill["issue_date"])
                due_date = date.fromisoformat(prefill["due_date"])
            except ValueError:
                return _render_new_editor(error="Špatný formát data.")

            prefill["taxable_supply_date"] = taxable_supply_date.isoformat()
            prefill["due_term"] = _infer_due_term_value(issue_date, due_date)

            items_total_cents = sum(int(item.get("line_total_cents") or 0) for item in items_payload)

            try:
                discount_cents = parse_money_to_cents(prefill.get("discount_amount"))
            except ValueError as exc:
                return _render_new_editor(error=str(exc))
            if discount_cents > max(items_total_cents, 0):
                return _render_new_editor(error="Sleva nesmí být vyšší než mezisoučet.")

            try:
                rounding_adj_cents = parse_money_to_signed_cents(prefill["rounding_adjustment"])
            except ValueError as exc:
                return _render_new_editor(error=str(exc))
            if bool(prefill.get("rounding_enabled")):
                rounding_adj_cents = compute_rounding_adjustment_cents(items_total_cents - discount_cents)
                prefill["rounding_adjustment"] = _cents_to_amount_str(rounding_adj_cents) if rounding_adj_cents != 0 else "0.00"

            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == int(prefill["contact_id"]))
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))
            if contact is None:
                return _render_new_editor(error="Kontakt neexistuje.")
            if not _party_payload_has_meaningful_values(prefill.get("buyer_party")):
                prefill["buyer_party"] = _normalize_party_payload(_party_payload_from_contact(contact))

            if normalized_document_type == "credit_note" and prefill.get("source_invoice_id") is not None:
                try:
                    source_invoice = db.scalar(
                        select(Invoice)
                        .where(Invoice.id == int(prefill["source_invoice_id"]))
                        .where(Invoice.subject_id == int(sid))
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))
                if source_invoice is None:
                    return _render_new_editor(error="Původní fakturu pro dobropis se nepodařilo najít.")
                available_credit_cents = _credit_note_available_cents(db, source_invoice=source_invoice)
                proposed_credit_cents = abs(int(items_total_cents - discount_cents + rounding_adj_cents))
                if proposed_credit_cents > available_credit_cents:
                    return _render_new_editor(
                        error=f"Dobropisem už bys překročil částku původní faktury. Zbývá dobropisovat maximálně {format_cents(available_credit_cents, str(source_invoice.currency or 'CZK'))}."
                    )

            selected_series: InvoiceSeries | None = None
            if prefill.get("series_id"):
                try:
                    selected_series = db.scalar(
                        select(InvoiceSeries)
                        .where(InvoiceSeries.id == int(prefill["series_id"]))
                        .where(InvoiceSeries.subject_id == sid)
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))
            if selected_series is None:
                selected_series = default_series

            selected_bank_account: SubjectBankAccount | None = None
            bank_account_raw = str(prefill.get("bank_account_id") or "").strip()
            if bank_account_raw:
                if not bank_account_raw.isdigit():
                    return _render_new_editor(error="Vybraný účet neexistuje.")
                try:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(bank_account_raw))
                        .where(SubjectBankAccount.subject_id == sid)
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))
                if selected_bank_account is None and default_bank_account is not None:
                    selected_bank_account = default_bank_account
                    prefill["bank_account_id"] = str(int(default_bank_account.id))
                if selected_bank_account is None:
                    return _render_new_editor(error="Vybraný účet neexistuje.")

            payment_method = str(prefill.get("payment_method") or "bank_transfer").strip().lower() or "bank_transfer"
            if payment_method not in {value for value, _label in INVOICE_PAYMENT_METHOD_OPTIONS}:
                return _render_new_editor(error="Neplatný způsob platby.")
            footer_mode, footer_text = _resolve_invoice_footer(
                subject=subject,
                footer_mode=prefill.get("footer_mode"),
                footer_text=prefill.get("footer_text"),
                language=prefill.get("invoice_language"),
            )
            create_as_issued = normalized_document_type == "invoice" and not recurring_mode and submit_action != "draft"

            invoice = Invoice(
                subject_id=sid,
                number=f"DRAFT-{uuid4().hex[:12]}",
                status="draft",
                issue_date=issue_date,
                taxable_supply_date=taxable_supply_date,
                due_date=due_date,
                currency=prefill["currency"].upper(),
                invoice_language=_normalize_invoice_language(prefill.get("invoice_language")),
                invoice_style=_normalize_invoice_style(prefill.get("invoice_style")),
                variable_symbol=prefill.get("variable_symbol") or _contact_fixed_variable_symbol(contact) or None,
                notes=prefill["notes"] or None,
                internal_notes=_mark_internal_recurring_template_note() if recurring_mode else None,
                payment_method=payment_method,
                footer_mode=footer_mode,
                footer_text=footer_text or None,
                document_type=normalized_document_type,
                source_invoice_id=(
                    int(prefill["source_invoice_id"])
                    if prefill.get("source_invoice_id") is not None
                    else None
                ),
                contact_id=contact.id,
                buyer_name_cache=contact.name,
                buyer_registration_no_cache=contact.ico or None,
                discount_cents=int(discount_cents),
                rounding_adjustment_cents=int(rounding_adj_cents),
                total_cents=int(rounding_adj_cents - discount_cents),
                series_id=(int(selected_series.id) if selected_series is not None else None),
            )

            if subject is not None and not recurring_mode:
                _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)

            db.add(invoice)
            try:
                db.flush()

                if not create_as_issued:
                    invoice.number = f"TPL-{int(invoice.id)}" if recurring_mode else f"DRAFT-{int(invoice.id)}"

                _sync_invoice_parties(
                    db,
                    invoice=invoice,
                    subject=subject,
                    contact=contact,
                    sync_existing=True,
                )
                _apply_manual_invoice_parties(
                    db,
                    invoice=invoice,
                    buyer_payload=prefill.get("buyer_party") or _party_payload_from_contact(contact),
                )
                _apply_invoice_bank_account_snapshot(
                    invoice,
                    account=selected_bank_account,
                    subject=subject,
                    allow_subject_fallback=bool(bank_account_raw and selected_bank_account is not None),
                )

                _replace_invoice_items(
                    db,
                    invoice_id=int(invoice.id),
                    items_payload=items_payload,
                )

                _recalc_invoice_total_cents(db, invoice=invoice)

                created_plan: RecurringInvoicePlan | None = None
                if recurring_mode:
                    try:
                        next_issue_date = date.fromisoformat(str(recurring_prefill.get("next_issue_date") or "").strip())
                    except Exception:
                        next_issue_date = date.today()
                    created_plan = RecurringInvoicePlan(
                        subject_id=int(sid),
                        template_invoice_id=int(invoice.id),
                        name=str(recurring_prefill.get("name") or "").strip() or f"Opakování pro {contact.name}",
                        interval_unit=_normalize_recurring_interval_unit(recurring_prefill.get("interval_unit")),
                        interval_count=max(1, int(recurring_prefill.get("interval_count") or 1)),
                        next_issue_date=next_issue_date,
                        due_in_days=max(0, int(recurring_prefill.get("due_in_days") or 14)),
                        is_active=True,
                        auto_issue=bool(recurring_prefill.get("auto_issue")),
                        auto_send=bool(recurring_prefill.get("auto_send")),
                        email_override=str(recurring_prefill.get("email_override") or "").strip() or None,
                    )
                    db.add(created_plan)
                    db.flush()

                if create_as_issued:
                    _issue_invoice_object(
                        db,
                        invoice=invoice,
                        subject=subject,
                        contact=contact,
                    )

                public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
                public_url_value = public_urls["view"] if public_urls else None
                _audit_log(
                    db,
                    action="invoice_created" if create_as_issued else "invoice_draft_created",
                    entity_type="invoice",
                    entity_id=int(invoice.id),
                    data={
                        "number": invoice.number,
                        "status": invoice.status,
                        "public_url": public_url_value,
                        "bank_account": (invoice.bank_account_number or invoice.bank_account_iban or ""),
                    },
                    subject_id=int(sid),
                )
                if created_plan is not None:
                    _audit_log(
                        db,
                        action="invoice_recurring_template_created",
                        entity_type="invoice",
                        entity_id=int(invoice.id),
                        data={
                            "template_invoice_id": int(invoice.id),
                            "recurring_plan_id": int(created_plan.id),
                            "next_issue_date": str(created_plan.next_issue_date),
                            "interval_unit": str(created_plan.interval_unit),
                            "interval_count": int(created_plan.interval_count),
                        },
                        subject_id=int(sid),
                    )

                db.commit()
                db.refresh(invoice)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=INVOICE_NEW_TITLE, db_error=str(exc))

            if recurring_mode:
                return RedirectResponse(url=f"/recurring?notice={quote('Automatická faktura uložená.', safe='')}", status_code=303)
            if not create_as_issued:
                notice_text = "Koncept je uložený. Finální číslo dostane až při vystavení."
                return RedirectResponse(url=f"/invoices/{invoice.id}?notice={quote(notice_text, safe='')}", status_code=303)
            return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)

        @app.post("/invoices/autosave")
        async def invoices_autosave(request: Request, db: Session = Depends(get_db)):
            await _verify_csrf(request)
            form = await request.form()
            recurring_mode = str(form.get("recurring_mode") or "").strip().lower() in {"1", "true", "on", "yes"}
            if recurring_mode:
                return JSONResponse(status_code=400, content={"ok": False, "detail": "Automatické faktury se průběžně neukládají."})

            normalized_document_type = _normalize_invoice_document_type(form.get("document_type"))
            try:
                sid = _current_subject_id()
                subject = _load_subject_for_current_session(db)
                is_vat_payer, default_currency = _subject_flags(db)
                subject_country = _invoice_subject_country(subject)
                default_vat_rate = _invoice_default_vat_rate(subject_country)
                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=normalized_document_type,
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})

            try:
                contact_id = int(form.get("contact_id")) if form.get("contact_id") else None
            except Exception:
                contact_id = None
            if not contact_id:
                return JSONResponse({"ok": False, "skipped": True, "detail": "Vyber odběratele, pak se koncept začne ukládat."})

            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == int(contact_id))
                    .where(Contact.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})
            if contact is None:
                return JSONResponse(status_code=404, content={"ok": False, "detail": "Kontakt neexistuje."})

            existing_invoice: Invoice | None = None
            raw_autosave_id = str(form.get("autosave_invoice_id") or "").strip()
            if raw_autosave_id:
                if not raw_autosave_id.isdigit():
                    return JSONResponse(status_code=400, content={"ok": False, "detail": "Neplatný koncept."})
                try:
                    existing_invoice = db.scalar(
                        select(Invoice)
                        .where(Invoice.id == int(raw_autosave_id))
                        .where(Invoice.subject_id == int(sid))
                        .where(Invoice.status == "draft")
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})
                if existing_invoice is None:
                    return JSONResponse(status_code=404, content={"ok": False, "detail": "Koncept se nepodařilo najít. Obnov stránku a zkus to znovu."})

            try:
                items_payload, raw_prefill_items = _parse_invoice_items_from_form(
                    form,
                    is_vat_payer=bool(is_vat_payer),
                    allow_negative_unit_price=normalized_document_type == "credit_note",
                    default_vat_rate=default_vat_rate,
                )
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"ok": False, "detail": str(exc)})

            def _has_autosave_content() -> bool:
                if items_payload:
                    return True
                for key in ("notes", "variable_symbol", "buyer_name", "buyer_street", "buyer_city", "buyer_ico", "buyer_dic"):
                    if str(form.get(key) or "").strip():
                        return True
                for row in raw_prefill_items:
                    if not _is_blank_invoice_item_row(row, is_vat_payer=bool(is_vat_payer), default_vat_rate=default_vat_rate):
                        return True
                return existing_invoice is not None

            if not _has_autosave_content():
                return JSONResponse({"ok": False, "skipped": True, "detail": "Koncept zatím není co ukládat."})

            today = date.today()
            try:
                issue_date = date.fromisoformat(str(form.get("issue_date") or today.isoformat()).strip())
                taxable_supply_date = date.fromisoformat(str(form.get("taxable_supply_date") or form.get("issue_date") or today.isoformat()).strip())
                due_date = date.fromisoformat(str(form.get("due_date") or (today + timedelta(days=14)).isoformat()).strip())
            except ValueError:
                return JSONResponse(status_code=422, content={"ok": False, "detail": "Špatný formát data."})

            currency = str(form.get("currency") or default_currency or "CZK").strip().upper() or "CZK"
            try:
                discount_cents = parse_money_to_cents(str(form.get("discount_amount") or "").strip())
                rounding_adj_cents = parse_money_to_signed_cents(str(form.get("rounding_adjustment") or "").strip())
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"ok": False, "detail": str(exc)})
            items_total_cents = sum(int(item.get("line_total_cents") or 0) for item in items_payload)
            if discount_cents > max(items_total_cents, 0):
                return JSONResponse(status_code=422, content={"ok": False, "detail": "Sleva nesmí být vyšší než mezisoučet."})
            if bool(form.get("rounding_enabled")):
                rounding_adj_cents = compute_rounding_adjustment_cents(items_total_cents - discount_cents)

            selected_series = default_series
            try:
                series_id = int(form.get("series_id")) if form.get("series_id") else None
            except Exception:
                series_id = None
            if series_id is not None:
                try:
                    selected_series = db.scalar(
                        select(InvoiceSeries)
                        .where(InvoiceSeries.id == int(series_id))
                        .where(InvoiceSeries.subject_id == int(sid))
                    ) or default_series
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})

            selected_bank_account: SubjectBankAccount | None = None
            bank_account_raw = str(form.get("bank_account_id") or "").strip()
            if bank_account_raw and bank_account_raw != "snapshot":
                if not bank_account_raw.isdigit():
                    return JSONResponse(status_code=422, content={"ok": False, "detail": "Vybraný účet neexistuje."})
                try:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(bank_account_raw))
                        .where(SubjectBankAccount.subject_id == int(sid))
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})
                if selected_bank_account is None:
                    return JSONResponse(status_code=422, content={"ok": False, "detail": "Vybraný účet neexistuje."})

            payment_method = str(form.get("payment_method") or "bank_transfer").strip().lower() or "bank_transfer"
            if payment_method not in {value for value, _label in INVOICE_PAYMENT_METHOD_OPTIONS}:
                return JSONResponse(status_code=422, content={"ok": False, "detail": "Neplatný způsob platby."})
            footer_mode, footer_text = _resolve_invoice_footer(
                subject=subject,
                footer_mode=form.get("footer_mode"),
                footer_text=str(form.get("footer_text") or "").strip(),
                language=form.get("invoice_language"),
            )

            invoice = existing_invoice or Invoice(
                subject_id=int(sid),
                number=f"DRAFT-{uuid4().hex[:12]}",
                status="draft",
                contact_id=int(contact.id),
                buyer_name_cache=str(contact.name or ""),
                buyer_registration_no_cache=contact.ico or None,
                series_id=(int(selected_series.id) if selected_series is not None else None),
                total_cents=0,
                discount_cents=0,
                rounding_adjustment_cents=0,
            )
            is_new = existing_invoice is None
            invoice.issue_date = issue_date
            invoice.taxable_supply_date = taxable_supply_date
            invoice.due_date = due_date
            invoice.currency = currency
            invoice.invoice_language = _normalize_invoice_language(form.get("invoice_language"))
            invoice.invoice_style = _normalize_invoice_style(form.get("invoice_style") or _default_invoice_style(subject))
            invoice.variable_symbol = _normalize_variable_symbol(form.get("variable_symbol")) or _contact_fixed_variable_symbol(contact) or None
            invoice.notes = str(form.get("notes") or "").strip() or None
            invoice.payment_method = payment_method
            invoice.footer_mode = footer_mode
            invoice.footer_text = footer_text or None
            invoice.document_type = normalized_document_type
            invoice.contact_id = int(contact.id)
            invoice.buyer_name_cache = str(contact.name or "")
            invoice.buyer_registration_no_cache = contact.ico or None
            invoice.discount_cents = int(discount_cents)
            invoice.rounding_adjustment_cents = int(rounding_adj_cents)
            invoice.series_id = int(selected_series.id) if selected_series is not None else None

            try:
                db.add(invoice)
                db.flush()
                if is_new:
                    invoice.number = f"DRAFT-{int(invoice.id)}"
                    if subject is not None:
                        _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)

                _sync_invoice_parties(
                    db,
                    invoice=invoice,
                    subject=subject,
                    contact=contact,
                    sync_existing=True,
                )
                _apply_manual_invoice_parties(
                    db,
                    invoice=invoice,
                    buyer_payload=_party_payload_from_form(
                        form,
                        prefix="buyer",
                        fallback=_party_payload_from_contact(contact),
                    ),
                )
                if selected_bank_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        invoice,
                        account=selected_bank_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                elif bank_account_raw != "snapshot":
                    invoice.bank_account_id = None
                    invoice.bank_account_label = None
                    invoice.bank_account_number = None
                    invoice.bank_account_iban = None
                    invoice.bank_account_bic = None
                    invoice.bank_account_country = None

                _replace_invoice_items(db, invoice_id=int(invoice.id), items_payload=items_payload)
                _recalc_invoice_total_cents(db, invoice=invoice)

                if is_new:
                    _audit_log(
                        db,
                        action="invoice_draft_autosaved",
                        entity_type="invoice",
                        entity_id=int(invoice.id),
                        data={"number": invoice.number, "status": invoice.status},
                        subject_id=int(sid),
                    )
                db.commit()
                db.refresh(invoice)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=503, content={"ok": False, "detail": _safe_db_error_message(exc)})

            return JSONResponse(
                {
                    "ok": True,
                    "invoice_id": int(invoice.id),
                    "number": str(invoice.number or ""),
                    "status": str(invoice.status or "draft"),
                    "detail_url": f"/invoices/{int(invoice.id)}",
                    "saved_at_label": "teď",
                    "created": bool(is_new),
                }
            )

        @app.post("/invoices/{invoice_id}/duplicate")
        async def invoices_duplicate(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()

            try:
                source_invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
                source_items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Duplikace faktury", db_error=str(exc))

            if source_invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")

            subject = _load_subject_for_current_session(db)
            today = date.today()
            due_term_days = max(0, (source_invoice.due_date - source_invoice.issue_date).days)
            source_document_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))

            try:
                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=source_document_type,
                )
                series_to_use = default_series
                if source_invoice.series_id is not None:
                    source_series = db.scalar(
                        select(InvoiceSeries)
                        .where(InvoiceSeries.id == int(source_invoice.series_id))
                        .where(InvoiceSeries.subject_id == int(sid))
                    )
                    if source_series is not None:
                        series_to_use = source_series

                selected_bank_account: SubjectBankAccount | None = None
                if source_invoice.bank_account_id is not None:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(source_invoice.bank_account_id))
                        .where(SubjectBankAccount.subject_id == int(sid))
                    )
                if selected_bank_account is None and str(getattr(source_invoice, "payment_method", "") or "bank_transfer") == "bank_transfer":
                    selected_bank_account = _default_subject_bank_account(
                        db,
                        subject_id=int(sid),
                        currency=str(getattr(source_invoice, "currency", None) or ""),
                    )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Duplikace faktury", db_error=str(exc))

            duplicated_invoice = Invoice(
                subject_id=int(sid),
                number=f"DRAFT-{uuid4().hex[:12]}",
                status="draft",
                issue_date=today,
                taxable_supply_date=today,
                due_date=today + timedelta(days=int(due_term_days)),
                currency=str(getattr(source_invoice, "currency", None) or "CZK").upper(),
                invoice_language=_normalize_invoice_language(getattr(source_invoice, "invoice_language", None)),
                invoice_style=_normalize_invoice_style(getattr(source_invoice, "invoice_style", None)),
                variable_symbol=_contact_fixed_variable_symbol(source_invoice.contact) or None,
                notes=(str(getattr(source_invoice, "notes", "") or "") or None),
                payment_method=str(getattr(source_invoice, "payment_method", "") or "bank_transfer"),
                footer_mode=str(getattr(source_invoice, "footer_mode", "") or _default_invoice_footer_mode(subject)),
                footer_text=(str(getattr(source_invoice, "footer_text", "") or "") or None),
                document_type=source_document_type,
                source_invoice_id=int(getattr(source_invoice, "source_invoice_id", 0) or 0) or None,
                contact_id=int(source_invoice.contact_id),
                buyer_name_cache=str(getattr(source_invoice, "buyer_name_cache", None) or source_invoice.contact.name or ""),
                buyer_registration_no_cache=str(getattr(source_invoice, "buyer_registration_no_cache", None) or source_invoice.contact.ico or "") or None,
                discount_cents=int(getattr(source_invoice, "discount_cents", 0) or 0),
                rounding_adjustment_cents=int(getattr(source_invoice, "rounding_adjustment_cents", 0) or 0),
                total_cents=0,
                series_id=(int(series_to_use.id) if series_to_use is not None else None),
            )

            if subject is not None:
                _maybe_ensure_invoice_public_link(db, invoice=duplicated_invoice, subject=subject)

            db.add(duplicated_invoice)
            try:
                db.flush()
                duplicated_invoice.number = f"DRAFT-{int(duplicated_invoice.id)}"

                _sync_invoice_parties(
                    db,
                    invoice=duplicated_invoice,
                    subject=subject,
                    contact=source_invoice.contact,
                    sync_existing=True,
                )

                if selected_bank_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        duplicated_invoice,
                        account=selected_bank_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                else:
                    duplicated_invoice.bank_account_id = None
                    duplicated_invoice.bank_account_label = getattr(source_invoice, "bank_account_label", None)
                    duplicated_invoice.bank_account_number = getattr(source_invoice, "bank_account_number", None)
                    duplicated_invoice.bank_account_iban = getattr(source_invoice, "bank_account_iban", None)
                    duplicated_invoice.bank_account_bic = getattr(source_invoice, "bank_account_bic", None)
                    duplicated_invoice.bank_account_country = getattr(source_invoice, "bank_account_country", None)

                _replace_invoice_items(
                    db,
                    invoice_id=int(duplicated_invoice.id),
                    items_payload=[
                        {
                            "description": str(getattr(item, "description", "") or ""),
                            "quantity": getattr(item, "quantity"),
                            "unit": _normalize_invoice_item_unit(getattr(item, "unit", "")),
                            "unit_price_cents": int(getattr(item, "unit_price_cents", 0) or 0),
                            "vat_rate": getattr(item, "vat_rate"),
                            "line_net_cents": int(getattr(item, "line_net_cents", 0) or 0),
                            "line_vat_cents": int(getattr(item, "line_vat_cents", 0) or 0),
                            "line_total_cents": int(getattr(item, "line_total_cents", 0) or 0),
                        }
                        for item in source_items
                    ],
                )

                _recalc_invoice_total_cents(db, invoice=duplicated_invoice)

                _audit_log(
                    db,
                    action="invoice_created",
                    entity_type="invoice",
                    entity_id=int(duplicated_invoice.id),
                    data={
                        "number": duplicated_invoice.number,
                        "status": duplicated_invoice.status,
                        "copied_from_invoice_id": int(source_invoice.id),
                        "copied_from_number": str(getattr(source_invoice, "number", "") or ""),
                        "public_url": bool(duplicated_invoice.public_token),
                    },
                    subject_id=int(sid),
                )

                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(source_invoice.id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se fakturu duplikovat."),
                    status_code=500,
                )

            copied_from_number = quote(str(getattr(source_invoice, "number", "") or ""), safe="")
            return RedirectResponse(
                url=f"/invoices/{duplicated_invoice.id}/edit?duplicated=1&from={copied_from_number}",
                status_code=303,
            )

        @app.post("/invoices/{invoice_id}/credit-note")
        async def invoices_create_credit_note(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()

            try:
                source_invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
                source_items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Nový dobropis", db_error=str(exc))

            if source_invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")

            source_document_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))
            if source_document_type != "invoice":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Dobropis lze vytvořit jen z běžné vystavené faktury.",
                    status_code=400,
                )
            if str(getattr(source_invoice, "status", "") or "").strip().lower() == "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Dobropis lze vytvořit až z vystavené faktury.",
                    status_code=400,
                )

            subject = _load_subject_for_current_session(db)
            today = date.today()
            due_term_days = max(0, (source_invoice.due_date - source_invoice.issue_date).days)

            try:
                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type="credit_note",
                )
                selected_bank_account: SubjectBankAccount | None = None
                if source_invoice.bank_account_id is not None:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(source_invoice.bank_account_id))
                        .where(SubjectBankAccount.subject_id == int(sid))
                    )
                if selected_bank_account is None and str(getattr(source_invoice, "payment_method", "") or "bank_transfer") == "bank_transfer":
                    selected_bank_account = _default_subject_bank_account(
                        db,
                        subject_id=int(sid),
                        currency=str(getattr(source_invoice, "currency", None) or "CZK"),
                    )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Nový dobropis", db_error=str(exc))

            credit_note = Invoice(
                subject_id=int(sid),
                number=f"DRAFT-{uuid4().hex[:12]}",
                status="draft",
                issue_date=today,
                taxable_supply_date=today,
                due_date=today + timedelta(days=int(due_term_days or 14)),
                currency=str(getattr(source_invoice, "currency", None) or "CZK").upper(),
                invoice_language=_normalize_invoice_language(getattr(source_invoice, "invoice_language", None)),
                invoice_style=_normalize_invoice_style(getattr(source_invoice, "invoice_style", None)),
                variable_symbol=(
                    _normalize_variable_symbol(getattr(source_invoice, "variable_symbol", None))
                    or _contact_fixed_variable_symbol(source_invoice.contact)
                    or None
                ),
                notes=f"Dobropis k faktuře {str(getattr(source_invoice, 'number', '') or '').strip()}",
                payment_method=str(getattr(source_invoice, "payment_method", "") or "bank_transfer"),
                footer_mode=str(getattr(source_invoice, "footer_mode", "") or _default_invoice_footer_mode(subject)),
                footer_text=(str(getattr(source_invoice, "footer_text", "") or "") or None),
                document_type="credit_note",
                source_invoice_id=int(source_invoice.id),
                contact_id=int(source_invoice.contact_id),
                buyer_name_cache=str(getattr(source_invoice, "buyer_name_cache", None) or source_invoice.contact.name or ""),
                buyer_registration_no_cache=str(getattr(source_invoice, "buyer_registration_no_cache", None) or source_invoice.contact.ico or "") or None,
                discount_cents=0,
                rounding_adjustment_cents=0,
                total_cents=0,
                series_id=(int(default_series.id) if default_series is not None else None),
            )

            if subject is not None:
                _maybe_ensure_invoice_public_link(db, invoice=credit_note, subject=subject)

            db.add(credit_note)
            try:
                db.flush()
                credit_note.number = f"DRAFT-{int(credit_note.id)}"

                _sync_invoice_parties(
                    db,
                    invoice=credit_note,
                    subject=subject,
                    contact=source_invoice.contact,
                    sync_existing=True,
                )

                if selected_bank_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        credit_note,
                        account=selected_bank_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )

                _replace_invoice_items(
                    db,
                    invoice_id=int(credit_note.id),
                    items_payload=[
                        {
                            "description": str(getattr(item, "description", "") or ""),
                            "quantity": getattr(item, "quantity"),
                            "unit": _normalize_invoice_item_unit(getattr(item, "unit", "")),
                            "unit_price_cents": -abs(int(getattr(item, "unit_price_cents", 0) or 0)),
                            "vat_rate": getattr(item, "vat_rate"),
                            "line_net_cents": -abs(int(getattr(item, "line_net_cents", 0) or 0)),
                            "line_vat_cents": -abs(int(getattr(item, "line_vat_cents", 0) or 0)),
                            "line_total_cents": -abs(int(getattr(item, "line_total_cents", 0) or 0)),
                        }
                        for item in source_items
                    ],
                )

                _recalc_invoice_total_cents(db, invoice=credit_note)

                _audit_log(
                    db,
                    action="invoice_created",
                    entity_type="invoice",
                    entity_id=int(credit_note.id),
                    data={
                        "number": credit_note.number,
                        "status": credit_note.status,
                        "document_type": "credit_note",
                        "source_invoice_id": int(source_invoice.id),
                        "source_invoice_number": str(getattr(source_invoice, "number", "") or ""),
                    },
                    subject_id=int(sid),
                )

                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(source_invoice.id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se připravit dobropis."),
                    status_code=500,
                )

            return RedirectResponse(url=f"/invoices/{credit_note.id}/edit", status_code=303)

        @app.post("/invoices/{invoice_id}/convert")
        async def invoices_convert(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            target_document_type = _normalize_invoice_document_type(form.get("target_document_type"))

            try:
                source_invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
                source_items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Převod dokladu", db_error=str(exc))

            if source_invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")

            source_document_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))
            allowed_targets = {value for value, _label in _invoice_conversion_targets(source_document_type)}
            if target_document_type not in allowed_targets:
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Tenhle převod dokladu teď nedává smysl.",
                    status_code=400,
                )

            subject = _load_subject_for_current_session(db)
            try:
                converted_invoice = _clone_invoice_from_template(
                    db,
                    source_invoice=source_invoice,
                    source_items=list(source_items),
                    subject=subject,
                    issue_date=date.today(),
                    due_date=date.today() + timedelta(days=max(0, int((source_invoice.due_date - source_invoice.issue_date).days or 14))),
                    document_type=target_document_type,
                    source_invoice_id=int(source_invoice.id),
                    notes_override=_default_conversion_notes(source_invoice, target_document_type=target_document_type),
                    render_tokens=False,
                )
                _audit_log(
                    db,
                    action="invoice_created",
                    entity_type="invoice",
                    entity_id=int(converted_invoice.id),
                    data={
                        "number": converted_invoice.number,
                        "status": converted_invoice.status,
                        "document_type": target_document_type,
                        "converted_from_invoice_id": int(source_invoice.id),
                        "converted_from_number": str(getattr(source_invoice, "number", "") or ""),
                    },
                    subject_id=int(sid),
                )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se připravit navazující doklad."),
                    status_code=500,
                )

            return RedirectResponse(url=f"/invoices/{converted_invoice.id}/edit", status_code=303)

        @app.post("/invoices/{invoice_id}/recurring")
        async def invoices_create_recurring_plan(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            try:
                source_invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Opakování dokladu", db_error=str(exc))
            if source_invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")

            document_type = _normalize_invoice_document_type(getattr(source_invoice, "document_type", "invoice"))
            if document_type == "credit_note":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Dobropis nedává smysl jako šablona pro opakování.",
                    status_code=400,
                )

            name = str(form.get("name") or "").strip() or f"Opakování {getattr(source_invoice, 'number', '')}".strip()
            interval_unit = _normalize_recurring_interval_unit(form.get("interval_unit"))
            try:
                interval_count = max(1, int(str(form.get("interval_count") or "1").strip() or "1"))
            except Exception:
                interval_count = 1
            try:
                next_issue_date = date.fromisoformat(str(form.get("next_issue_date") or "").strip())
            except Exception:
                next_issue_date = date.today()
            try:
                due_in_days = max(0, int(str(form.get("due_in_days") or "14").strip() or "14"))
            except Exception:
                due_in_days = 14

            plan = RecurringInvoicePlan(
                subject_id=int(sid),
                template_invoice_id=int(source_invoice.id),
                name=name,
                interval_unit=interval_unit,
                interval_count=interval_count,
                next_issue_date=next_issue_date,
                due_in_days=due_in_days,
                is_active=True,
                auto_issue=bool(form.get("auto_issue")),
                auto_send=bool(form.get("auto_send")),
                email_override=str(form.get("email_override") or "").strip() or None,
            )
            db.add(plan)
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se uložit opakování."),
                    status_code=500,
                )
            return RedirectResponse(url=f"/invoices/{invoice_id}?notice={quote('Opakování uložené.', safe='')}", status_code=303)

        @app.get("/recurring", response_class=HTMLResponse)
        def recurring_list(
            request: Request,
            db: Session = Depends(get_db),
            notice: str | None = None,
            error: str | None = None,
        ):
            sid = _current_subject_id()
            try:
                plans = db.scalars(
                    select(RecurringInvoicePlan)
                    .options(selectinload(RecurringInvoicePlan.template_invoice).selectinload(Invoice.contact))
                    .where(RecurringInvoicePlan.subject_id == int(sid))
                    .order_by(RecurringInvoicePlan.is_active.desc(), RecurringInvoicePlan.next_issue_date.asc(), RecurringInvoicePlan.id.asc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Automatické faktury", db_error=str(exc))

            return templates.TemplateResponse(
                request,
                "recurring/list.html",
                {
                    "recurring_plans": [_recurring_plan_summary(plan) for plan in plans],
                    "recurring_create_url": "/invoices/new?recurring_mode=1",
                    "notice": notice,
                    "error": error,
                },
            )

        def _load_recurring_plan_for_current_subject(db: Session, *, plan_id: int, subject_id: int) -> RecurringInvoicePlan | None:
            return db.scalar(
                select(RecurringInvoicePlan)
                .options(selectinload(RecurringInvoicePlan.template_invoice).selectinload(Invoice.contact))
                .where(RecurringInvoicePlan.id == int(plan_id))
                .where(RecurringInvoicePlan.subject_id == int(subject_id))
            )

        def _build_recurring_template_editor_context(
            request: Request,
            db: Session,
            *,
            invoice: Invoice,
            plan_id: int,
            subject_id: int,
            notice: str | None = None,
            error: str | None = None,
        ) -> dict:
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            items = db.scalars(
                select(InvoiceItem)
                .where(InvoiceItem.invoice_id == int(invoice.id))
                .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
            ).all()
            contacts = db.scalars(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .order_by(Contact.name.asc())
            ).all()
            default_series = _get_or_create_default_invoice_series(
                db,
                subject_id=int(subject_id),
                document_type=document_type,
            )
            series_list = db.scalars(
                select(InvoiceSeries)
                .where(InvoiceSeries.subject_id == int(subject_id))
                .order_by(InvoiceSeries.name.asc())
            ).all()
            bank_accounts_rows = _list_subject_bank_accounts(db, subject_id=int(subject_id))
            subject = _load_subject_for_current_session(db)
            public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
            seller_party_prefill = _party_payload_from_snapshot_or_fallback(
                db,
                invoice_id=int(invoice.id),
                role="seller",
                fallback=_party_payload_from_subject(subject),
            )
            buyer_party_prefill = _party_payload_from_snapshot_or_fallback(
                db,
                invoice_id=int(invoice.id),
                role="buyer",
                fallback=_party_payload_from_contact(invoice.contact),
            )

            is_vat_payer, _default_currency = _subject_flags(db)
            subject_country = _invoice_subject_country(subject)
            vat_rate_options = _invoice_vat_rate_options(subject_country)
            default_vat_rate = _invoice_default_vat_rate(subject_country)
            _sync_series_list_for_year(
                db,
                subject_id=int(subject_id),
                series_list=series_list,
                year=invoice.issue_date.year,
            )
            selected_series_for_prefill = _pick_invoice_series_for_preview(
                series_list,
                selected_id=(int(invoice.series_id) if invoice.series_id else None),
                default_series=default_series,
            )
            items_total_cents = sum(int(it.line_total_cents or 0) for it in items)
            discount_cents = int(getattr(invoice, "discount_cents", 0) or 0)
            rounding_adj_cents = int(invoice.rounding_adjustment_cents or 0)
            computed_rounding_cents = compute_rounding_adjustment_cents(items_total_cents - discount_cents)
            rounding_enabled = bool(rounding_adj_cents != 0 and rounding_adj_cents == computed_rounding_cents)
            default_bank_account = _default_subject_bank_account(
                db,
                subject_id=int(subject_id),
                currency=str(getattr(invoice, "currency", None) or ""),
            )
            bank_account_prefill = ""
            if invoice.bank_account_id is not None:
                bank_account_prefill = str(int(invoice.bank_account_id))
            elif (invoice.bank_account_number or invoice.bank_account_iban):
                bank_account_prefill = "snapshot"
            elif default_bank_account is not None:
                bank_account_prefill = str(int(default_bank_account.id))

            preview_year = invoice.issue_date.year
            final_number_preview = (
                invoice.number
                if str(invoice.status or "").strip().lower() != "draft"
                else _series_next_number_preview(selected_series_for_prefill, year=preview_year)
            )
            prefill = {
                "contact_id": invoice.contact_id,
                "document_type": document_type,
                "source_invoice_id": int(getattr(invoice, "source_invoice_id", 0) or 0) or None,
                "issue_date": invoice.issue_date.isoformat(),
                "taxable_supply_date": (_invoice_taxable_supply_date(invoice) or invoice.issue_date).isoformat(),
                "due_date": invoice.due_date.isoformat(),
                "currency": invoice.currency,
                "invoice_language": _normalize_invoice_language(getattr(invoice, "invoice_language", None)),
                "invoice_style": _normalize_invoice_style(getattr(invoice, "invoice_style", None)),
                "series_id": int(invoice.series_id) if invoice.series_id else (int(default_series.id) if default_series else None),
                "bank_account_id": bank_account_prefill,
                "payment_method": str(getattr(invoice, "payment_method", "") or "bank_transfer"),
                "footer_mode": str(getattr(invoice, "footer_mode", "") or _default_invoice_footer_mode(subject)),
                "footer_text": str(
                    getattr(invoice, "footer_text", None)
                    or _invoice_footer_text_for_mode(getattr(invoice, "footer_mode", None), subject=subject)
                ),
                "due_term": _infer_due_term_value(invoice.issue_date, invoice.due_date),
                "discount_amount": _cents_to_amount_str(getattr(invoice, "discount_cents", 0)) if int(getattr(invoice, "discount_cents", 0) or 0) != 0 else "",
                "rounding_enabled": rounding_enabled,
                "rounding_adjustment": _cents_to_amount_str(invoice.rounding_adjustment_cents) if int(invoice.rounding_adjustment_cents or 0) != 0 else ("0.00" if rounding_enabled else ""),
                "notes": invoice.notes or "",
                "variable_symbol": _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)),
                "final_number_preview": final_number_preview,
                "seller_party": seller_party_prefill,
                "buyer_party": buyer_party_prefill,
            }
            raw_prefill_items = [
                _invoice_item_prefill_from_model(item, is_vat_payer=bool(is_vat_payer), default_vat_rate=default_vat_rate) for item in items
            ]
            prefill, prefill_items = _apply_invoice_editor_summary(
                prefill=prefill,
                prefill_items=raw_prefill_items,
                is_vat_payer=bool(is_vat_payer),
                allow_negative_unit_price=document_type == "credit_note",
                min_rows=max(1, len(raw_prefill_items) or 1),
                default_vat_rate=default_vat_rate,
            )
            back_url = f"/recurring/{int(plan_id)}/edit"
            next_url = f"{back_url}?template_saved=1"
            return {
                "invoice": invoice,
                "contacts": contacts,
                "prefill": prefill,
                "prefill_items": prefill_items,
                "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(subject_id)),
                "series_options": _build_invoice_series_options(series_list, year=preview_year),
                "account_options": _build_bank_account_options(bank_accounts_rows, current_invoice=invoice),
                "currency_options": _build_currency_options(prefill.get("currency")),
                "catalog_items": _list_invoice_catalog_items(db, subject_id=int(subject_id), currency=prefill.get("currency"), limit=12),
                "is_vat_payer": bool(is_vat_payer),
                "vat_rate_options": vat_rate_options,
                "default_vat_rate": default_vat_rate,
                "subject_country": subject_country,
                "due_term_options": INVOICE_DUE_TERM_OPTIONS,
                "item_unit_options": INVOICE_ITEM_UNIT_OPTIONS,
                "payment_method_options": INVOICE_PAYMENT_METHOD_OPTIONS,
                "invoice_language_options": INVOICE_LANGUAGE_OPTIONS,
                "invoice_style_options": INVOICE_STYLE_OPTIONS,
                "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                "back_url": back_url,
                "form_action": f"/invoices/{int(invoice.id)}/edit?next={quote(next_url, safe='')}",
                "public_url": public_urls["view"] if public_urls else None,
                "page_title": "Šablona automatické faktury",
                "page_subtitle": "Tady upravíš odběratele, položky, proměnné, poznámku i patičku. Z téhle šablony se pak vystavují opakované doklady.",
                "submit_label": "Uložit šablonu",
                "series_locked": str(invoice.status or "").strip().lower() != "draft",
                "recurring_mode": False,
                "notice": notice,
                "error": error,
            }

        def _render_recurring_edit(
            request: Request,
            db: Session,
            *,
            plan: RecurringInvoicePlan,
            plan_error: str | None = None,
            plan_notice: str | None = None,
            template_notice: str | None = None,
            template_error: str | None = None,
            status_code: int = 200,
        ):
            context = {
                "plan": _recurring_plan_summary(plan),
                "recurring_interval_options": RECURRING_INTERVAL_OPTIONS,
                "plan_error": plan_error,
                "plan_notice": plan_notice,
            }
            template_invoice = getattr(plan, "template_invoice", None)
            if template_invoice is not None:
                context.update(
                    _build_recurring_template_editor_context(
                        request,
                        db,
                        invoice=template_invoice,
                        plan_id=int(plan.id),
                        subject_id=int(plan.subject_id),
                        notice=template_notice,
                        error=template_error,
                    )
                )
            return templates.TemplateResponse(
                request,
                "recurring/edit.html",
                context,
                status_code=status_code,
            )

        @app.get("/recurring/{plan_id}/edit", response_class=HTMLResponse)
        def recurring_plan_edit(plan_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                plan = _load_recurring_plan_for_current_subject(db, plan_id=int(plan_id), subject_id=int(sid))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Upravit automatickou fakturu", db_error=str(exc))
            if plan is None:
                raise HTTPException(status_code=404, detail="Recurring plan not found")
            plan_notice = "Plán automatické faktury uložený." if str(request.query_params.get("plan_saved") or "") == "1" else None
            template_notice = "Šablona automatické faktury uložená." if str(request.query_params.get("template_saved") or "") == "1" else None
            return _render_recurring_edit(request, db, plan=plan, plan_notice=plan_notice, template_notice=template_notice)

        @app.post("/recurring/{plan_id}/edit")
        async def recurring_plan_update(plan_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                plan = _load_recurring_plan_for_current_subject(db, plan_id=int(plan_id), subject_id=int(sid))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Upravit automatickou fakturu", db_error=str(exc))
            if plan is None:
                raise HTTPException(status_code=404, detail="Recurring plan not found")

            form = await request.form()
            name = str(form.get("name") or "").strip()
            try:
                interval_count = max(1, int(str(form.get("interval_count") or "1").strip() or "1"))
            except Exception:
                interval_count = 1
            try:
                due_in_days = max(0, int(str(form.get("due_in_days") or "14").strip() or "14"))
            except Exception:
                due_in_days = 14
            try:
                next_issue_date = date.fromisoformat(str(form.get("next_issue_date") or "").strip())
            except Exception:
                return _render_recurring_edit(
                    request,
                    db,
                    plan=plan,
                    plan_error="Vyber platné datum dalšího vystavení.",
                    status_code=400,
                )
            auto_issue = bool(form.get("auto_issue"))
            auto_send = bool(form.get("auto_send"))
            if auto_send and not auto_issue:
                return _render_recurring_edit(
                    request,
                    db,
                    plan=plan,
                    plan_error="Automatické odeslání e-mailem dává smysl jen u dokladu, který se rovnou vystaví.",
                    status_code=400,
                )

            plan.name = name or str(getattr(plan, "name", "") or "").strip() or "Automatická faktura"
            plan.interval_unit = _normalize_recurring_interval_unit(form.get("interval_unit"))
            plan.interval_count = interval_count
            plan.next_issue_date = next_issue_date
            plan.due_in_days = due_in_days
            plan.auto_issue = auto_issue
            plan.auto_send = auto_send
            plan.email_override = str(form.get("email_override") or "").strip() or None
            plan.is_active = bool(form.get("is_active"))
            db.add(plan)
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Upravit automatickou fakturu", db_error=str(exc), status_code=500)
            return RedirectResponse(url=f"/recurring/{int(plan.id)}/edit?plan_saved=1", status_code=303)

        @app.post("/recurring/{plan_id}/run")
        async def recurring_plan_run(plan_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), "/recurring")
            result = _process_recurring_plans(
                db,
                request=request,
                subject_id=int(sid),
                plan_id=int(plan_id),
                force=True,
            )
            if result["errors"]:
                return RedirectResponse(url=f"{next_url}?error={quote(str(result['errors'][0]), safe='')}", status_code=303)
            created_ids = list(result.get("created_invoice_ids") or [])
            message = "Opakování spuštěné."
            if created_ids:
                message = f"Vytvořen doklad #{created_ids[0]}."
            return RedirectResponse(url=f"{next_url}?notice={quote(message, safe='')}", status_code=303)

        @app.post("/recurring/{plan_id}/toggle")
        async def recurring_plan_toggle(plan_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), "/recurring")
            try:
                plan = db.scalar(
                    select(RecurringInvoicePlan)
                    .where(RecurringInvoicePlan.id == int(plan_id))
                    .where(RecurringInvoicePlan.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Opakování dokladu", db_error=str(exc))
            if plan is None:
                raise HTTPException(status_code=404, detail="Recurring plan not found")
            plan.is_active = not bool(getattr(plan, "is_active", False))
            db.add(plan)
            db.commit()
            return RedirectResponse(url=f"{next_url}?notice={quote('Opakování upravené.', safe='')}", status_code=303)

        @app.post("/recurring/{plan_id}/delete")
        async def recurring_plan_delete(plan_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), "/recurring")
            try:
                plan = db.scalar(
                    select(RecurringInvoicePlan)
                    .where(RecurringInvoicePlan.id == int(plan_id))
                    .where(RecurringInvoicePlan.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Opakování dokladu", db_error=str(exc))
            if plan is None:
                raise HTTPException(status_code=404, detail="Recurring plan not found")
            template_invoice = db.scalar(
                select(Invoice)
                .where(Invoice.id == int(getattr(plan, "template_invoice_id", 0) or 0))
                .where(Invoice.subject_id == int(sid))
            )
            db.delete(plan)
            if _is_internal_recurring_template_invoice(template_invoice):
                db.delete(template_invoice)
            db.commit()
            return RedirectResponse(url=f"{next_url}?notice={quote('Opakování smazané.', safe='')}", status_code=303)

        @app.get("/invoices/item-suggestions")
        def invoice_item_suggestions(
            q: str = "",
            limit: int = 8,
            currency: str | None = None,
            exclude_invoice_id: int | None = None,
            db: Session = Depends(get_db),
        ):
            sid = _current_subject_id()
            suggestions = _list_invoice_item_suggestions(
                db,
                subject_id=sid,
                query=q,
                limit=limit,
                currency=currency,
                exclude_invoice_id=exclude_invoice_id,
            )
            return JSONResponse({"suggestions": suggestions})

        @app.get("/invoices/catalog-items")
        def invoice_catalog_items(
            q: str = "",
            limit: int = 12,
            currency: str | None = None,
            db: Session = Depends(get_db),
        ):
            sid = _current_subject_id()
            items = _list_invoice_catalog_items(
                db,
                subject_id=sid,
                query=q,
                limit=limit,
                currency=currency,
            )
            return JSONResponse({"items": items})

        @app.post("/invoices/catalog-items")
        async def invoice_catalog_items_save(request: Request, db: Session = Depends(get_db)):
            await _verify_csrf(request)
            sid = _current_subject_id()
            form = await request.form()
            subject = _load_subject_for_current_session(db)
            is_vat_payer, default_currency = _subject_flags(db, subject_override=subject)
            default_vat_rate = _invoice_default_vat_rate(_invoice_subject_country(subject))
            try:
                item, created = _save_invoice_catalog_item(
                    db,
                    subject_id=sid,
                    description=str(form.get("description") or ""),
                    quantity=str(form.get("quantity") or "1"),
                    unit=str(form.get("unit") or ""),
                    unit_price=str(form.get("unit_price") or ""),
                    vat_rate=str(form.get("vat_rate") or (default_vat_rate if is_vat_payer else "0")),
                    currency=str(form.get("currency") or default_currency),
                    is_vat_payer=bool(is_vat_payer),
                )
                db.commit()
            except ValueError as exc:
                db.rollback()
                return JSONResponse(status_code=400, content={"detail": str(exc)})
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=500, content={"detail": _safe_db_error_message(exc)})
            return JSONResponse({"ok": True, "created": bool(created), "item": item})

        @app.post("/invoices/catalog-items/{item_id}/delete")
        async def invoice_catalog_items_delete(item_id: int, request: Request, db: Session = Depends(get_db)):
            await _verify_csrf(request)
            sid = _current_subject_id()
            try:
                item = db.scalar(
                    select(InvoiceCatalogItem)
                    .where(InvoiceCatalogItem.id == int(item_id))
                    .where(InvoiceCatalogItem.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return JSONResponse(status_code=500, content={"detail": _safe_db_error_message(exc)})
            if item is None:
                return JSONResponse(status_code=404, content={"detail": "Catalog item not found"})
            try:
                db.delete(item)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=500, content={"detail": _safe_db_error_message(exc)})
            return JSONResponse({"ok": True, "deleted_id": int(item_id)})

        @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
        def invoices_detail(
            invoice_id: int,
            request: Request,
            notice: str | None = None,
            db: Session = Depends(get_db),
        ):
            return _render_invoice_detail(request=request, db=db, invoice_id=invoice_id, notice=notice)

        # ------------------------------------------------------------------
        # Phase-21: public invoice sharing (enable/disable/rotate token)
        # ------------------------------------------------------------------
        @app.post("/invoices/{invoice_id}/public/enable")
        async def invoice_public_enable(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), f"/invoices/{invoice_id}")

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})
            if str(invoice.status or "").strip().lower() == "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Koncept nemá veřejný odkaz. Nejdřív doklad vystav.",
                    status_code=400,
                )

            try:
                subject = db.scalar(select(Subject).where(Subject.id == int(sid)))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if subject is None:
                return JSONResponse(status_code=404, content={"detail": "Subject not found"})

            _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)

            db.add(subject)
            db.add(invoice)
            _audit_log(
                db,
                request=request,
                action="invoice_public_enabled",
                entity_type="invoice",
                entity_id=int(invoice.id),
                subject_id=int(sid),
                user_id=_current_user_id_or_none(),
                data={"number": str(invoice.number or "")},
            )
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se zapnout veřejný odkaz."),
                    status_code=500,
                )

            return RedirectResponse(url=next_url, status_code=303)

        @app.post("/invoices/{invoice_id}/public/disable")
        async def invoice_public_disable(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), f"/invoices/{invoice_id}")

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            invoice.public_token = None
            db.add(invoice)
            _audit_log(
                db,
                request=request,
                action="invoice_public_disabled",
                entity_type="invoice",
                entity_id=int(invoice.id),
                subject_id=int(sid),
                user_id=_current_user_id_or_none(),
                data={"number": str(invoice.number or "")},
            )
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vypnout veřejný odkaz."),
                    status_code=500,
                )

            return RedirectResponse(url=next_url, status_code=303)

        @app.post("/invoices/{invoice_id}/public/rotate")
        async def invoice_public_rotate(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            next_url = _safe_next_url(form.get("next"), f"/invoices/{invoice_id}")

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})
            if str(invoice.status or "").strip().lower() == "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Koncept nemá veřejný odkaz. Nejdřív doklad vystav.",
                    status_code=400,
                )

            try:
                subject = db.scalar(select(Subject).where(Subject.id == int(sid)))
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if subject is not None:
                ensure_subject_public_username(db, subject=subject)
                db.add(subject)

            invoice.public_token = generate_unique_invoice_public_token(db)
            db.add(invoice)
            _audit_log(
                db,
                request=request,
                action="invoice_public_rotated",
                entity_type="invoice",
                entity_id=int(invoice.id),
                subject_id=int(sid),
                user_id=_current_user_id_or_none(),
                data={"number": str(invoice.number or "")},
            )
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error=_safe_operation_error(exc, fallback="Nepodařilo se obnovit token."),
                    status_code=500,
                )

            return RedirectResponse(url=next_url, status_code=303)

        # ------------------------------------------------------------------
        # Phase-22: SMTP email sending + invoice_emails log
        # ------------------------------------------------------------------
        # Phase-22: SMTP email sending + invoice_emails log
        # ------------------------------------------------------------------
        @app.post("/invoices/{invoice_id}/email")
        async def invoice_send_email(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()
            # NOTE: we intentionally ignore `next` for now and redirect back to the invoice.

            to_raw = str(form.get("to_email") or "").strip()
            cc_raw = str(form.get("cc_email") or "").strip()
            subj_raw = str(form.get("subject") or "").strip()
            body_raw = str(form.get("body") or "").strip()

            attach_pdf = bool(form.get("attach_pdf"))
            include_public_link = bool(form.get("include_public_link"))

            # Load invoice + contact.
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            if str(invoice.status or "").strip().lower() == "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="E-mail lze poslat až po vystavení faktury.",
                    status_code=400,
                )

            # SMTP configuration and From identity.
            subject = _load_subject_for_current_session(db)
            mail_ctx = _mail_identity_context(db, subject=subject, request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "").strip()
            signature_name = str(mail_ctx.get("signature_name") or "").strip()

            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )

            if not smtp_is_configured(smtp_cfg):
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="SMTP není nastavené. Doplň SMTP_HOST a případně SMTP_USERNAME/SMTP_PASSWORD (viz .env.example).",
                    prefill_email={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            if not looks_like_email(from_email):
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Chybí odesílatel (From). Nastav email subjektu v /settings nebo SMTP_FROM_EMAIL.",
                    prefill_email={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            recipients = split_recipients(to_raw)
            if not recipients or not all(looks_like_email(r) for r in recipients):
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Neplatný e-mail příjemce.",
                    prefill_email={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            cc_recipients = split_recipients(cc_raw)
            if cc_recipients and not all(looks_like_email(r) for r in cc_recipients):
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Neplatný e-mail v kopii (CC).",
                    prefill_email={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            # Default subject/body if user left them empty.
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            invoice_language = _normalize_invoice_language(getattr(invoice, "invoice_language", None))
            document_label = _invoice_document_type_label(document_type, invoice_language)
            subj = subj_raw or _invoice_document_email_subject(document_type, invoice.number, invoice_language)
            body = body_raw
            if not body:
                total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
                body = (
                    f"{_invoice_text('email_hello', invoice_language)}\n\n"
                    f"{_invoice_text('email_attached', invoice_language)} {document_label.lower()} {invoice.number} {_invoice_text('reminder_amount', invoice_language)} {total_str}.\n"
                    f"{_invoice_text('email_due', invoice_language)}: {invoice.due_date}.\n\n"
                    f"{_invoice_text('email_best_regards', invoice_language)}\n"
                    f"{signature_name or from_name}\n"
                )

            # Optionally append public link (if enabled for the invoice).
            public_url: str | None = None
            if include_public_link:
                public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
                public_url = public_urls["view"] if public_urls else None
            if public_url and public_url not in body:
                body = body.rstrip() + f"\n\n{public_url}\n"

            # Prepare PDF attachment (optional).
            pdf_attachment: tuple[str, bytes] | None = None
            if attach_pdf:
                pdf_bytes: bytes | None = None
                if _invoice_cached_pdf_is_fresh(invoice):
                    cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
                    if cached is not None and bytes(cached).startswith(b"%PDF"):
                        pdf_bytes = bytes(cached)

                if pdf_bytes is None:
                    try:
                        items = db.scalars(
                            select(InvoiceItem)
                            .where(InvoiceItem.invoice_id == int(invoice.id))
                            .order_by(InvoiceItem.sort_order.asc())
                        ).all()
                    except SQLAlchemyError as exc:  # type: ignore[misc]
                        return _render_invoice_detail(
                            request=request,
                            db=db,
                            invoice_id=int(invoice_id),
                            error=_safe_operation_error(exc, fallback="Nelze načíst položky pro PDF."),
                            prefill_email={
                                "to_email": to_raw,
                                "cc_email": cc_raw,
                                "subject": subj,
                                "body": body,
                                "attach_pdf": attach_pdf,
                                "include_public_link": include_public_link,
                            },
                            status_code=500,
                        )

                    ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))

                    # Prefer HTML → PDF via WeasyPrint; fall back to ReportLab.
                    try:
                        html = templates.get_template("invoices/print.html").render(
                            {
                                "request": request,
                                **ctx,
                                "pdf_mode": True,
                                "app_css": _load_app_css(),
                            }
                        )
                        pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                    except Exception:
                        pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)
                        pdf_bytes = render_invoice_pdf_bytes(pdf_data)

                    # Persist for issued invoices (best effort).
                    if pdf_bytes is not None:
                        _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=bytes(pdf_bytes))

                if pdf_bytes is not None:
                    safe_no = re.sub(r"[^A-Za-z0-9._-]+", "_", str(invoice.number or "invoice"))
                    filename = f"{safe_no}.pdf"
                    pdf_attachment = (filename, bytes(pdf_bytes))

            # Create log row first so failures are recorded.
            email_row = InvoiceEmail(
                invoice_id=int(invoice.id),
                kind="invoice",
                from_email=from_email,
                to_email=_format_recipient_log_value(to_emails=recipients, cc_emails=cc_recipients),
                subject=subj[:255],
                body=body,
                status="queued",
                sent_at=None,
                message_id=None,
                error_message=None,
            )
            db.add(email_row)
            try:
                db.commit()
                db.refresh(email_row)
            except SQLAlchemyError:
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Nepodařilo se uložit log e-mailu do DB.",
                    prefill_email={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj,
                        "body": body,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=500,
                )

            # Send.
            try:
                msg = build_email_message(
                    from_email=from_email,
                    from_name=from_name,
                    to_emails=recipients,
                    cc_emails=cc_recipients,
                    subject=subj,
                    body=body,
                    attachment_pdf=pdf_attachment,
                )
                message_id, _debug = send_via_smtp(smtp_cfg, msg)
                email_row.status = "sent"
                email_row.sent_at = utc_now()
                email_row.message_id = (message_id or "")[:255] if message_id else None
                email_row.error_message = None
            except Exception as exc:
                email_row.status = "error"
                email_row.sent_at = None
                logging.getLogger("fakturek").error(
                    "Invoice email failed for invoice %s (error_type=%s)",
                    invoice_id,
                    type(exc).__name__,
                )
                email_row.error_message = "E-mail se nepodařilo odeslat."
            finally:
                db.add(email_row)
                try:
                    db.commit()
                except SQLAlchemyError:
                    db.rollback()

            if str(email_row.status) == "sent":
                try:
                    old_invoice_status = str(getattr(invoice, "status", "") or "").strip().lower()
                    if old_invoice_status == "issued":
                        changed, _error = _apply_invoice_status_transition(invoice, new_status="sent")
                        if changed:
                            db.add(invoice)
                    _audit_log(
                        db,
                        request=request,
                        action="invoice_email_sent",
                        entity_type="invoice",
                        entity_id=int(invoice.id),
                        subject_id=int(sid),
                        user_id=_current_user_id_or_none(),
                        data={
                            "to_email": ", ".join(recipients),
                            "cc_email": ", ".join(cc_recipients),
                            "subject": subj,
                            "attach_pdf": bool(attach_pdf),
                            "include_public_link": bool(include_public_link),
                            "status_before": old_invoice_status,
                            "status_after": str(getattr(invoice, "status", "") or "").strip().lower(),
                        },
                    )
                    db.commit()
                except SQLAlchemyError:
                    db.rollback()
                ok_msg = quote("E-mail odeslán.", safe="")
                return RedirectResponse(url=f"/invoices/{invoice_id}?notice={ok_msg}", status_code=303)

            # Error: stay on the invoice page with the filled form.
            return _render_invoice_detail(
                request=request,
                db=db,
                invoice_id=int(invoice_id),
                error="E-mail se nepodařilo odeslat. Zkontrolujte nastavení SMTP a zkuste to znovu.",
                prefill_email={
                    "to_email": to_raw,
                    "cc_email": cc_raw,
                    "subject": subj,
                    "body": body,
                    "attach_pdf": attach_pdf,
                    "include_public_link": include_public_link,
                },
                status_code=500,
            )

        # ------------------------------------------------------------------
        # Phase-23: payment reminders (upomínky)
        # ------------------------------------------------------------------
        @app.post("/invoices/{invoice_id}/reminder")
        async def invoice_send_reminder(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            form = await request.form()

            next_url = _safe_next_url(form.get("next"), f"/invoices/{invoice_id}")
            quick = bool(form.get("quick"))

            # For manual sends we accept custom fields; for quick sends we use defaults.
            to_raw = str(form.get("to_email") or "").strip()
            cc_raw = str(form.get("cc_email") or "").strip()
            subj_raw = str(form.get("subject") or "").strip()
            body_raw = str(form.get("body") or "").strip()

            attach_pdf = bool(form.get("attach_pdf")) if (not quick) else True
            include_public_link = bool(form.get("include_public_link")) if (not quick) else True

            def _redirect_with(param: str, message: str) -> RedirectResponse:
                enc = quote(str(message or "").strip() or "OK", safe="")
                sep = "&" if "?" in next_url else "?"
                return RedirectResponse(url=f"{next_url}{sep}{param}={enc}", status_code=303)

            # Load invoice + contact.
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.contact.has(Contact.subject_id == int(sid)))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            inv_status = str(invoice.status or "").strip().lower()
            if inv_status == "draft":
                if quick:
                    return _redirect_with("error", "Upomínku lze poslat až po vystavení faktury.")
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Upomínku lze poslat až po vystavení faktury.",
                    status_code=400,
                )

            today_local = date.today()
            if inv_status == "paid":
                if quick:
                    return _redirect_with("error", "Upomínku nelze poslat – faktura je zaplacená.")
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Upomínku nelze poslat – faktura je zaplacená.",
                    status_code=400,
                )

            if not (invoice.due_date < today_local):
                if quick:
                    return _redirect_with("error", "Upomínku lze poslat jen po splatnosti.")
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Upomínku lze poslat jen po splatnosti.",
                    status_code=400,
                )

            days_overdue = int((today_local - invoice.due_date).days)

            # SMTP configuration and From identity.
            subject = _load_subject_for_current_session(db)
            mail_ctx = _mail_identity_context(db, subject=subject, request=request)
            from_email = str(mail_ctx.get("from_email") or "").strip()
            from_name = str(mail_ctx.get("from_name") or "").strip()
            signature_name = str(mail_ctx.get("signature_name") or "").strip()

            smtp_cfg = SMTPConfig(
                host=settings.smtp_host,
                port=int(settings.smtp_port or 0),
                username=settings.smtp_username,
                password=settings.smtp_password,
                use_tls=bool(settings.smtp_use_tls),
                use_starttls=bool(settings.smtp_use_starttls),
                timeout_seconds=float(settings.smtp_timeout_seconds or 10.0),
                from_email=from_email,
                from_name=from_name,
            )

            if not smtp_is_configured(smtp_cfg):
                if quick:
                    return _redirect_with(
                        "error",
                        "SMTP není nastavené. Doplň SMTP_HOST a případně SMTP_USERNAME/SMTP_PASSWORD (viz .env.example).",
                    )
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="SMTP není nastavené. Doplň SMTP_HOST a případně SMTP_USERNAME/SMTP_PASSWORD (viz .env.example).",
                    prefill_reminder={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            if not looks_like_email(from_email):
                if quick:
                    return _redirect_with(
                        "error",
                        "Chybí odesílatel (From). Nastav email subjektu v /settings nebo SMTP_FROM_EMAIL.",
                    )
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Chybí odesílatel (From). Nastav email subjektu v /settings nebo SMTP_FROM_EMAIL.",
                    prefill_reminder={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            # Determine recipients.
            if quick:
                to_raw = (getattr(invoice.contact, "email", "") or "").strip()
                cc_raw = ""

            recipients = split_recipients(to_raw)
            if not recipients or not all(looks_like_email(r) for r in recipients):
                if quick:
                    return _redirect_with("error", "Neplatný e-mail příjemce (kontakt nemá e-mail?).")
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Neplatný e-mail příjemce.",
                    prefill_reminder={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            cc_recipients = split_recipients(cc_raw)
            if (not quick) and cc_recipients and not all(looks_like_email(r) for r in cc_recipients):
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Neplatný e-mail v kopii (CC).",
                    prefill_reminder={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj_raw,
                        "body": body_raw,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=400,
                )

            # Default subject/body if user left them empty.
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            invoice_language = _normalize_invoice_language(getattr(invoice, "invoice_language", None))
            document_label = _invoice_document_type_label(document_type, invoice_language)
            subj = subj_raw or f"{_invoice_text('reminder_subject_prefix', invoice_language)}: {document_label} {invoice.number}"
            body = body_raw
            if not body:
                total_str = format_cents(int(invoice.total_cents or 0), str(invoice.currency or "CZK"))
                payment_account = _invoice_bank_account_payload(invoice, subject=subject)
                bank_account = payment_account.display if payment_account is not None else ""
                lines = [
                    _invoice_text("email_hello", invoice_language),
                    "",
                    f"{_invoice_text('reminder_intro', invoice_language)} {document_label.lower()} {invoice.number} {_invoice_text('reminder_amount', invoice_language)} {total_str}.",
                    f"{_invoice_text('email_due', invoice_language)}: {invoice.due_date} ({days_overdue} {_invoice_text('reminder_overdue', invoice_language)}).",
                    "",
                    _invoice_text("reminder_request", invoice_language),
                ]
                if bank_account:
                    lines += ["", f"{_invoice_text('email_account_number', invoice_language)}: {bank_account}", f"{_invoice_text('email_reference', invoice_language)}: {_invoice_variable_symbol(invoice, contact=invoice.contact) or invoice.number}"]
                lines += ["", _invoice_text("email_best_regards", invoice_language), signature_name or from_name or ""]
                body = "\n".join([ln for ln in lines if ln is not None])

            # Optionally append public link (if enabled for the invoice).
            public_url: str | None = None
            if include_public_link:
                public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)
                public_url = public_urls["view"] if public_urls else None
            if public_url and public_url not in body:
                body = body.rstrip() + f"\n\n{public_url}\n"

            # Prepare PDF attachment (optional).
            pdf_attachment: tuple[str, bytes] | None = None
            if attach_pdf:
                pdf_bytes: bytes | None = None
                if _invoice_cached_pdf_is_fresh(invoice):
                    cached = read_pdf_bytes(pdf_storage_root, str(invoice.pdf_path))
                    if cached is not None and bytes(cached).startswith(b"%PDF"):
                        pdf_bytes = bytes(cached)

                if pdf_bytes is None:
                    try:
                        items = db.scalars(
                            select(InvoiceItem)
                            .where(InvoiceItem.invoice_id == int(invoice.id))
                            .order_by(InvoiceItem.sort_order.asc())
                        ).all()
                    except SQLAlchemyError as exc:  # type: ignore[misc]
                        if quick:
                            return _redirect_with(
                                "error",
                                _safe_operation_error(exc, fallback="Nelze načíst položky pro PDF."),
                            )
                        return _render_invoice_detail(
                            request=request,
                            db=db,
                            invoice_id=int(invoice_id),
                            error=_safe_operation_error(exc, fallback="Nelze načíst položky pro PDF."),
                            prefill_reminder={
                                "to_email": to_raw,
                                "cc_email": cc_raw,
                                "subject": subj,
                                "body": body,
                                "attach_pdf": attach_pdf,
                                "include_public_link": include_public_link,
                            },
                            status_code=500,
                        )

                    ctx = _build_invoice_print_context(db, invoice=invoice, items=list(items))

                    # Prefer HTML → PDF via WeasyPrint; fall back to ReportLab.
                    try:
                        html = templates.get_template("invoices/print.html").render(
                            {
                                "request": request,
                                **ctx,
                                "pdf_mode": True,
                                "app_css": _load_app_css(),
                            }
                        )
                        pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
                    except Exception:
                        pdf_data = _invoice_pdf_data_from_context(invoice=invoice, ctx=ctx)
                        pdf_bytes = render_invoice_pdf_bytes(pdf_data)

                    # Persist for issued invoices (best effort).
                    if pdf_bytes is not None:
                        _persist_invoice_pdf_best_effort(db, invoice=invoice, pdf_bytes=bytes(pdf_bytes))

                if pdf_bytes is not None:
                    safe_no = re.sub(r"[^A-Za-z0-9._-]+", "_", str(invoice.number or "invoice"))
                    filename = f"{safe_no}.pdf"
                    pdf_attachment = (filename, bytes(pdf_bytes))

            # Create log row first so failures are recorded.
            email_row = InvoiceEmail(
                invoice_id=int(invoice.id),
                kind="reminder",
                from_email=from_email,
                to_email=_format_recipient_log_value(to_emails=recipients, cc_emails=cc_recipients),
                subject=subj[:255],
                body=body,
                status="queued",
                sent_at=None,
                message_id=None,
                error_message=None,
            )
            db.add(email_row)
            try:
                db.commit()
                db.refresh(email_row)
            except SQLAlchemyError:
                db.rollback()
                if quick:
                    return _redirect_with("error", "Nepodařilo se uložit log e-mailu do DB.")
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=int(invoice_id),
                    error="Nepodařilo se uložit log e-mailu do DB.",
                    prefill_reminder={
                        "to_email": to_raw,
                        "cc_email": cc_raw,
                        "subject": subj,
                        "body": body,
                        "attach_pdf": attach_pdf,
                        "include_public_link": include_public_link,
                    },
                    status_code=500,
                )

            # Send.
            try:
                msg = build_email_message(
                    from_email=from_email,
                    from_name=from_name,
                    to_emails=recipients,
                    cc_emails=cc_recipients,
                    subject=subj,
                    body=body,
                    attachment_pdf=pdf_attachment,
                )
                message_id, _debug = send_via_smtp(smtp_cfg, msg)
                email_row.status = "sent"
                email_row.sent_at = utc_now()
                email_row.message_id = (message_id or "")[:255] if message_id else None
                email_row.error_message = None
            except Exception as exc:
                email_row.status = "error"
                email_row.sent_at = None
                logging.getLogger("fakturek").error(
                    "Invoice reminder email failed for invoice %s (error_type=%s)",
                    invoice_id,
                    type(exc).__name__,
                )
                email_row.error_message = "E-mail se nepodařilo odeslat."
            finally:
                db.add(email_row)
                try:
                    db.commit()
                except SQLAlchemyError:
                    db.rollback()

            if str(email_row.status) == "sent":
                # Update invoice reminder timestamp (best effort).
                try:
                    invoice.reminder_sent_at = utc_now()
                    db.add(invoice)
                    _audit_log(
                        db,
                        request=request,
                        action="invoice_reminder_sent",
                        entity_type="invoice",
                        entity_id=int(invoice.id),
                        subject_id=int(sid),
                        user_id=_current_user_id_or_none(),
                        data={
                            "to_email": ", ".join(recipients),
                            "cc_email": ", ".join(cc_recipients),
                            "subject": subj,
                            "attach_pdf": bool(attach_pdf),
                            "include_public_link": bool(include_public_link),
                            "days_overdue": int(days_overdue),
                        },
                    )
                    db.commit()
                except SQLAlchemyError:
                    try:
                        db.rollback()
                    except Exception:
                        pass

                return _redirect_with("notice", "Upomínka odeslána.")

            # Failure.
            if quick:
                return _redirect_with(
                    "error",
                    "Upomínku se nepodařilo odeslat. Zkontrolujte nastavení SMTP a zkuste to znovu.",
                )

            return _render_invoice_detail(
                request=request,
                db=db,
                invoice_id=int(invoice_id),
                error="Upomínku se nepodařilo odeslat. Zkontrolujte nastavení SMTP a zkuste to znovu.",
                prefill_reminder={
                    "to_email": to_raw,
                    "cc_email": cc_raw,
                    "subject": subj,
                    "body": body,
                    "attach_pdf": attach_pdf,
                    "include_public_link": include_public_link,
                },
                status_code=500,
            )

        @app.post("/invoices/{invoice_id}/issue")
        def invoices_issue(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            """Issue (finalize) a draft invoice."""

            sid = _current_subject_id()

            if settings.auth_required:
                user_id = request.session.get("user_id")
                if not user_id:
                    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
                link = db.scalar(
                    select(UserSubject).where(
                        UserSubject.user_id == int(user_id),
                        UserSubject.subject_id == int(sid),
                    )
                )
                if link is None or not bool(getattr(link, "can_issue", False)):
                    return JSONResponse(status_code=403, content={"detail": "Access denied"})

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == int(invoice_id))
                    .where(Invoice.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            document_label = _invoice_document_type_label(document_type)

            if invoice.status != "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error=f"{document_label} už není v konceptu – nelze ji znovu vystavit.",
                    status_code=400,
                )

            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == int(invoice.contact_id))
                    .where(Contact.subject_id == int(sid))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))

            subject = _load_subject_for_current_session(db)
            if document_type == "credit_note" and getattr(invoice, "source_invoice_id", None) is not None:
                try:
                    source_invoice = db.scalar(
                        select(Invoice)
                        .where(Invoice.id == int(invoice.source_invoice_id))
                        .where(Invoice.subject_id == int(sid))
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
                if source_invoice is None:
                    return _render_invoice_detail(
                        request=request,
                        db=db,
                        invoice_id=invoice_id,
                        error="Původní fakturu pro dobropis se nepodařilo najít.",
                        status_code=400,
                    )
                available_credit_cents = _credit_note_available_cents(
                    db,
                    source_invoice=source_invoice,
                    current_credit_note_id=int(invoice.id),
                )
                proposed_credit_cents = abs(int(getattr(invoice, "total_cents", 0) or 0))
                if proposed_credit_cents > available_credit_cents:
                    return _render_invoice_detail(
                        request=request,
                        db=db,
                        invoice_id=invoice_id,
                        error=f"Dobropisem už bys překročil částku původní faktury. Zbývá dobropisovat maximálně {format_cents(available_credit_cents, str(source_invoice.currency or 'CZK'))}.",
                        status_code=400,
                    )

            series: InvoiceSeries | None = None
            if invoice.series_id is not None:
                try:
                    series = db.scalar(
                        select(InvoiceSeries)
                        .where(InvoiceSeries.id == int(invoice.series_id))
                        .where(InvoiceSeries.subject_id == int(sid))
                    )
                except SQLAlchemyError:
                    series = None

            if series is None:
                try:
                    series = _get_or_create_default_invoice_series(
                        db,
                        subject_id=sid,
                        document_type=document_type,
                    )
                except Exception as exc:
                    db.rollback()
                    return _render_invoice_detail(
                        request=request,
                        db=db,
                        invoice_id=invoice_id,
                        error=_safe_operation_error(exc, fallback="Nepodařilo se připravit číselnou řadu."),
                        status_code=500,
                    )
                invoice.series_id = int(series.id)

            selected_bank_account: SubjectBankAccount | None = None
            if invoice.bank_account_id is not None:
                try:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(invoice.bank_account_id))
                        .where(SubjectBankAccount.subject_id == int(sid))
                    )
                except SQLAlchemyError:
                    selected_bank_account = None
            elif not (invoice.bank_account_number or invoice.bank_account_iban):
                try:
                    selected_bank_account = _default_subject_bank_account(db, subject_id=sid)
                except Exception:
                    selected_bank_account = None

            try:
                number = _allocate_next_invoice_number(
                    db,
                    subject_id=sid,
                    series_id=int(series.id),
                    invoice_id=int(invoice.id),
                    issue_date=invoice.issue_date,
                )
            except Exception as exc:
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vygenerovat číslo dokladu."),
                    status_code=500,
                )

            invoice.number = number
            if not _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)):
                invoice.variable_symbol = _contact_fixed_variable_symbol(contact) or variable_symbol_from_invoice_number(number)
            invoice.status = "issued"
            invoice.issued_at = utc_now()

            try:
                if subject is not None:
                    _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)

                _sync_invoice_parties(
                    db,
                    invoice=invoice,
                    subject=subject,
                    contact=contact,
                    sync_existing=True,
                )
                if selected_bank_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        invoice,
                        account=selected_bank_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                elif not (invoice.bank_account_number or invoice.bank_account_iban):
                    _apply_invoice_bank_account_snapshot(
                        invoice,
                        account=None,
                        subject=subject,
                        allow_subject_fallback=True,
                    )

                _recalc_invoice_total_cents(db, invoice=invoice)
                _audit_log(
                    db,
                    action="invoice_issued",
                    entity_type="invoice",
                    entity_id=int(invoice.id),
                    data={
                        "number": invoice.number,
                        "document_type": document_type,
                        "issue_date": str(invoice.issue_date),
                        "public_token": bool((invoice.public_token or "").strip()),
                    },
                    subject_id=int(sid),
                )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error=_safe_operation_error(exc, fallback="Nepodařilo se vystavit doklad."),
                    status_code=500,
                )

            _regenerate_invoice_pdf_best_effort(request, db, invoice_id=int(invoice_id), subject_id=int(sid))
            return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

        @app.post("/invoices/{invoice_id}/status")
        async def invoices_set_status(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            new_status = (form.get("status") or "").strip().lower()
            next_url = _safe_next_url(form.get("next"), f"/invoices/{invoice_id}")

            sid = _current_subject_id()

            if new_status not in {"draft", "issued", "sent", "paid", "cancelled"}:
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Neplatný stav faktury.",
                    status_code=400,
                )

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            old_status = str(invoice.status or "").strip().lower()
            previous_paid_on = invoice.paid_on
            mark_unpaid = old_status == "paid" and new_status in {"issued", "sent"} and str(form.get("unpaid") or "").strip() == "1"
            paid_on_value: date | None = None
            if new_status == "paid":
                raw_paid_on = str(form.get("paid_on") or "").strip()
                if raw_paid_on:
                    try:
                        paid_on_value = date.fromisoformat(raw_paid_on)
                    except ValueError:
                        return _render_invoice_detail(
                            request=request,
                            db=db,
                            invoice_id=invoice_id,
                            error="Datum úhrady není platné.",
                            status_code=400,
                        )
                else:
                    paid_on_value = date.today()

            changed, error = _apply_invoice_status_transition(
                invoice,
                new_status=new_status,
                paid_on=paid_on_value,
            )
            if not changed:
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error=error or "Neplatný stav faktury.",
                    status_code=400,
                )

            try:
                _audit_log(
                    db,
                    action="invoice_status_changed",
                    entity_type="invoice",
                    entity_id=int(invoice.id),
                    data={
                        "from": old_status,
                        "to": new_status,
                        "paid_on": str(invoice.paid_on) if invoice.paid_on is not None else None,
                        "unpaid": bool(mark_unpaid),
                        "previous_paid_on": str(previous_paid_on) if mark_unpaid and previous_paid_on is not None else None,
                    },
                    subject_id=int(sid),
                )
                payment: Payment | None = None
                if new_status == "paid":
                    payment = _ensure_manual_invoice_payment(
                        db,
                        invoice=invoice,
                        paid_on=invoice.paid_on,
                        source="manual_status",
                    )
                    _audit_log(
                        db,
                        action="invoice_payment_recorded",
                        entity_type="payment",
                        entity_id=int(payment.id),
                        data={
                            "invoice_id": int(invoice.id),
                            "invoice_number": str(getattr(invoice, "number", "") or ""),
                            "amount_cents": int(getattr(payment, "amount_cents", 0) or 0),
                            "paid_on": str(getattr(payment, "paid_on", "") or ""),
                            "source": "manual_status",
                        },
                        subject_id=int(sid),
                    )
                elif mark_unpaid:
                    removed_payments = _remove_unlinked_manual_invoice_payments(db, invoice=invoice)
                    if removed_payments:
                        _audit_log(
                            db,
                            action="invoice_manual_payments_removed",
                            entity_type="invoice",
                            entity_id=int(invoice.id),
                            data={
                                "invoice_id": int(invoice.id),
                                "invoice_number": str(getattr(invoice, "number", "") or ""),
                                "removed": int(removed_payments),
                                "source": "manual_unpaid",
                            },
                            subject_id=int(sid),
                        )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))

            if new_status != "draft":
                _regenerate_invoice_pdf_best_effort(request, db, invoice_id=int(invoice_id), subject_id=int(sid))

            return RedirectResponse(url=next_url, status_code=303)

        @app.post("/invoices/bulk-status")
        async def invoices_bulk_status(request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            action = str(form.get("action") or "").strip().lower()
            next_url = _safe_next_url(form.get("next"), "/invoices")

            if action not in {value for value, _label in BULK_INVOICE_ACTION_OPTIONS}:
                return RedirectResponse(
                    url=_with_query_params(next_url, error="Vyber platnou hromadnou akci."),
                    status_code=303,
                )

            selected_ids: list[int] = []
            for raw in form.getlist("invoice_ids"):
                try:
                    candidate = int(str(raw).strip())
                except Exception:
                    continue
                if candidate > 0 and candidate not in selected_ids:
                    selected_ids.append(candidate)

            if not selected_ids:
                return RedirectResponse(
                    url=_with_query_params(next_url, error="Nejdřív vyber aspoň jednu fakturu."),
                    status_code=303,
                )

            sid = _current_subject_id()
            try:
                invoices = db.scalars(
                    select(Invoice)
                    .where(Invoice.subject_id == int(sid))
                    .where(Invoice.id.in_(selected_ids))
                    .order_by(Invoice.issue_date.desc(), Invoice.id.desc())
                ).all()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))

            found_map = {int(getattr(invoice, "id", 0) or 0): invoice for invoice in invoices}
            changed_rows: list[tuple[int, str, str]] = []
            skipped_count = 0
            paid_on_value: date | None = None
            if action == "paid":
                raw_paid_on = str(form.get("paid_on") or "").strip()
                if raw_paid_on:
                    try:
                        paid_on_value = date.fromisoformat(raw_paid_on)
                    except ValueError:
                        return RedirectResponse(
                            url=_with_query_params(next_url, error="Datum úhrady není platné."),
                            status_code=303,
                        )
                else:
                    paid_on_value = date.today()

            for invoice_id in selected_ids:
                invoice = found_map.get(int(invoice_id))
                if invoice is None:
                    skipped_count += 1
                    continue
                old_status = str(getattr(invoice, "status", "") or "").strip().lower()
                changed, _error, applied_target = _apply_bulk_invoice_action(
                    invoice,
                    action=action,
                    paid_on=paid_on_value,
                )
                if not changed or not applied_target:
                    skipped_count += 1
                    continue
                changed_rows.append((int(invoice.id), old_status, applied_target))

            if not changed_rows:
                return RedirectResponse(
                    url=_with_query_params(
                        next_url,
                        error=f"Hromadná akce „{_bulk_invoice_action_label(action)}“ se nedala použít na žádný vybraný doklad.",
                    ),
                    status_code=303,
                )

            try:
                for invoice_id, old_status, applied_target in changed_rows:
                    invoice = found_map.get(int(invoice_id))
                    if invoice is None:
                        continue
                    _audit_log(
                        db,
                        action="invoice_status_changed",
                        entity_type="invoice",
                        entity_id=int(invoice.id),
                        data={
                            "from": old_status,
                            "to": applied_target,
                            "bulk": True,
                            "paid_on": str(invoice.paid_on) if getattr(invoice, "paid_on", None) is not None else None,
                        },
                        subject_id=int(sid),
                    )
                    if applied_target == "paid":
                        payment = _ensure_manual_invoice_payment(
                            db,
                            invoice=invoice,
                            paid_on=getattr(invoice, "paid_on", None),
                            source="bulk_status",
                        )
                        _audit_log(
                            db,
                            action="invoice_payment_recorded",
                            entity_type="payment",
                            entity_id=int(payment.id),
                            data={
                                "invoice_id": int(invoice.id),
                                "invoice_number": str(getattr(invoice, "number", "") or ""),
                                "amount_cents": int(getattr(payment, "amount_cents", 0) or 0),
                                "paid_on": str(getattr(payment, "paid_on", "") or ""),
                                "source": "bulk_status",
                            },
                            subject_id=int(sid),
                        )
                    elif old_status == "paid" and applied_target in {"issued", "sent"}:
                        removed_payments = _remove_unlinked_manual_invoice_payments(db, invoice=invoice)
                        if removed_payments:
                            _audit_log(
                                db,
                                action="invoice_manual_payments_removed",
                                entity_type="invoice",
                                entity_id=int(invoice.id),
                                data={
                                    "invoice_id": int(invoice.id),
                                    "invoice_number": str(getattr(invoice, "number", "") or ""),
                                    "removed": int(removed_payments),
                                    "source": "bulk_unpaid",
                                },
                                subject_id=int(sid),
                            )
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return RedirectResponse(
                    url=_with_query_params(
                        next_url,
                        error=_safe_operation_error(exc, fallback="Hromadnou úpravu se nepodařilo uložit."),
                    ),
                    status_code=303,
                )

            for invoice_id, _old_status, applied_target in changed_rows:
                if applied_target != "draft":
                    _regenerate_invoice_pdf_best_effort(request, db, invoice_id=int(invoice_id), subject_id=int(sid))

            notice = f"{_bulk_invoice_action_label(action)} u {len(changed_rows)} dokladů."
            error_message = None
            if skipped_count:
                error_message = f"{skipped_count} vybraných dokladů jsem přeskočil, protože pro ně tahle akce nedávala smysl."

            return RedirectResponse(
                url=_with_query_params(next_url, notice=notice, error=error_message),
                status_code=303,
            )

        @app.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
        def invoices_print(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            return _render_invoice_print(request=request, db=db, invoice_id=invoice_id)

        @app.get("/invoices/{invoice_id}/cash-receipt", response_class=HTMLResponse)
        def invoices_cash_receipt(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            payment_method = str(getattr(invoice, "payment_method", "") or "bank_transfer").strip().lower()
            if payment_method != "cash":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Potvrzení o přijetí hotovosti je dostupné jen pro hotovostní doklady.",
                    status_code=400,
                )
            if str(getattr(invoice, "status", "") or "").strip().lower() != "paid":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Potvrzení o přijetí hotovosti můžeš vytisknout až po označení dokladu jako zaplaceného.",
                    status_code=400,
                )

            subject = _load_subject_for_current_session(db)
            seller = _party_payload_from_subject(subject)
            buyer = _party_payload_from_contact(invoice.contact)
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            document_label = _invoice_document_type_label(document_type)
            payment_date = getattr(invoice, "paid_on", None) or getattr(invoice, "issue_date", None) or date.today()
            receipt_number = f"PPD-{invoice.number}"
            amount_cents = int(getattr(invoice, "total_cents", 0) or 0)

            copies = [
                {"label": "Originál pro plátce"},
                {"label": "Kopie pro příjemce"},
            ]

            return templates.TemplateResponse(
                request,
                "invoices/cash_receipt.html",
                {
                    "invoice": invoice,
                    "document_label": document_label,
                    "receipt_number": receipt_number,
                    "payment_date": payment_date,
                    "seller": seller,
                    "buyer": buyer,
                    "amount_cents": amount_cents,
                    "copies": copies,
                    "payment_method_label": _invoice_payment_method_label(payment_method),
                    "cash_limit_cents": 27_000_000,
                    "cash_limit_exceeded": str(getattr(invoice, "currency", "") or "CZK").upper() == "CZK" and amount_cents > 27_000_000,
                    "back_url": f"/invoices/{invoice_id}",
                },
            )

        @app.get("/invoices/{invoice_id}/cash-receipt/pdf")
        def invoices_cash_receipt_pdf(invoice_id: int, request: Request, download: bool = False, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError:  # type: ignore[misc]
                return _invoice_pdf_error_response(
                    request,
                    title="PDF potvrzení hotovosti",
                    message="Databáze není dostupná – nelze načíst doklad.",
                )
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            payment_method = str(getattr(invoice, "payment_method", "") or "bank_transfer").strip().lower()
            if payment_method != "cash":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Potvrzení o přijetí hotovosti je dostupné jen pro hotovostní doklady.",
                    status_code=400,
                )
            if str(getattr(invoice, "status", "") or "").strip().lower() != "paid":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Potvrzení o přijetí hotovosti můžeš vytisknout až po označení dokladu jako zaplaceného.",
                    status_code=400,
                )

            subject = _load_subject_for_current_session(db)
            context = {
                "request": request,
                "invoice": invoice,
                "document_label": _invoice_document_type_label(getattr(invoice, "document_type", "invoice")),
                "receipt_number": f"PPD-{invoice.number}",
                "payment_date": getattr(invoice, "paid_on", None) or getattr(invoice, "issue_date", None) or date.today(),
                "seller": _party_payload_from_subject(subject),
                "buyer": _party_payload_from_contact(invoice.contact),
                "amount_cents": int(getattr(invoice, "total_cents", 0) or 0),
                "copies": [
                    {"label": "Originál pro plátce"},
                    {"label": "Kopie pro příjemce"},
                ],
                "payment_method_label": _invoice_payment_method_label(payment_method),
                "cash_limit_cents": 27_000_000,
                "cash_limit_exceeded": str(getattr(invoice, "currency", "") or "CZK").upper() == "CZK" and int(getattr(invoice, "total_cents", 0) or 0) > 27_000_000,
                "back_url": f"/invoices/{invoice_id}",
                "pdf_mode": True,
                "app_css": _load_app_css(),
            }
            try:
                html = templates.get_template("invoices/cash_receipt.html").render(context)
                pdf_bytes = render_html_pdf_bytes(html, base_url=project_root)
            except Exception as exc:
                return _invoice_pdf_error_response(
                    request,
                    title="PDF potvrzení hotovosti",
                    message=_safe_operation_error(exc, fallback="Potvrzení se nepodařilo převést do PDF."),
                    invoice_number=invoice.number,
                )
            disp = (
                content_disposition_attachment(f"pokladni-doklad-{invoice.number}")
                if bool(download)
                else content_disposition_inline(f"pokladni-doklad-{invoice.number}")
            )
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
            )

        @app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
        def invoices_edit(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))
            duplicated_mode = str(request.query_params.get("duplicated") or "").strip() == "1"
            duplicated_from = str(request.query_params.get("from") or "").strip()

            duplicate_page_title = None
            duplicate_page_subtitle = None
            if duplicated_mode:
                if document_type == "quote":
                    duplicate_page_title = "Nová zduplikovaná nabídka"
                    duplicate_page_subtitle = "Vznikla nová kopie nabídky. Zkontroluj údaje, případně je uprav, a pak ji můžeš rovnou používat dál."
                elif document_type == "credit_note":
                    duplicate_page_title = "Nový zduplikovaný dobropis"
                    duplicate_page_subtitle = "Vznikla nová kopie dobropisu. Zkontroluj údaje a uprav ji podle potřeby."
                elif document_type == "proforma":
                    duplicate_page_title = "Nová zduplikovaná zálohová faktura"
                    duplicate_page_subtitle = "Vznikla nová kopie zálohové faktury. Zkontroluj údaje a uprav ji podle potřeby."
                else:
                    duplicate_page_title = "Nová zduplikovaná faktura"
                    duplicate_page_subtitle = "Vznikla nová kopie faktury. Zkontroluj údaje, případně je uprav, a pak ji můžeš rovnou poslat nebo vystavit dál."
                if duplicated_from:
                    duplicate_page_subtitle += f" Vychází z dokladu {duplicated_from}."
            duplicate_form_action = f"/invoices/{invoice.id}/edit"
            if duplicated_mode:
                duplicate_form_action = f"/invoices/{invoice.id}/edit/issue?duplicated=1"
                if duplicated_from:
                    duplicate_form_action += f"&from={quote(duplicated_from, safe='')}"

            try:
                items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()

                contacts = db.scalars(
                    select(Contact)
                    .where(Contact.subject_id == sid)
                    .order_by(Contact.name.asc())
                ).all()

                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=document_type,
                )
                series_list = db.scalars(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.subject_id == sid)
                    .order_by(InvoiceSeries.name.asc())
                ).all()
                bank_accounts_rows = _list_subject_bank_accounts(db, subject_id=sid)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))

            subject = _load_subject_for_current_session(db)
            public_urls = _invoice_public_urls_for_request(request, invoice=invoice, subject=subject)

            is_vat_payer, _default_currency = _subject_flags(db)
            subject_country = _invoice_subject_country(subject)
            vat_rate_options = _invoice_vat_rate_options(subject_country)
            default_vat_rate = _invoice_default_vat_rate(subject_country)
            _sync_series_list_for_year(
                db,
                subject_id=sid,
                series_list=series_list,
                year=invoice.issue_date.year,
            )
            selected_series_for_prefill = _pick_invoice_series_for_preview(
                series_list,
                selected_id=(int(invoice.series_id) if invoice.series_id else None),
                default_series=default_series,
            )
            items_total_cents = sum(int(it.line_total_cents or 0) for it in items)
            discount_cents = int(getattr(invoice, "discount_cents", 0) or 0)
            rounding_adj_cents = int(invoice.rounding_adjustment_cents or 0)
            computed_rounding_cents = compute_rounding_adjustment_cents(items_total_cents - discount_cents)
            rounding_enabled = bool(rounding_adj_cents != 0 and rounding_adj_cents == computed_rounding_cents)
            default_bank_account = _default_subject_bank_account(
                db,
                subject_id=sid,
                currency=str(getattr(invoice, "currency", None) or ""),
            )
            bank_account_prefill = ""
            if invoice.bank_account_id is not None:
                bank_account_prefill = str(int(invoice.bank_account_id))
            elif (invoice.bank_account_number or invoice.bank_account_iban):
                bank_account_prefill = "snapshot"
            elif default_bank_account is not None:
                bank_account_prefill = str(int(default_bank_account.id))
            seller_party_prefill = _party_payload_from_snapshot_or_fallback(
                db,
                invoice_id=int(invoice.id),
                role="seller",
                fallback=_party_payload_from_subject(subject),
            )
            buyer_party_prefill = _party_payload_from_snapshot_or_fallback(
                db,
                invoice_id=int(invoice.id),
                role="buyer",
                fallback=_party_payload_from_contact(invoice.contact),
            )

            preview_year = invoice.issue_date.year
            final_number_preview = (
                invoice.number
                if str(invoice.status or "").strip().lower() != "draft"
                else _series_next_number_preview(selected_series_for_prefill, year=preview_year)
            )
            prefill = {
                "contact_id": invoice.contact_id,
                "document_type": document_type,
                "source_invoice_id": int(getattr(invoice, "source_invoice_id", 0) or 0) or None,
                "issue_date": invoice.issue_date.isoformat(),
                "taxable_supply_date": (_invoice_taxable_supply_date(invoice) or invoice.issue_date).isoformat(),
                "due_date": invoice.due_date.isoformat(),
                "currency": invoice.currency,
                "invoice_language": _normalize_invoice_language(getattr(invoice, "invoice_language", None)),
                "invoice_style": _normalize_invoice_style(getattr(invoice, "invoice_style", None)),
                "series_id": int(invoice.series_id) if invoice.series_id else (int(default_series.id) if default_series else None),
                "bank_account_id": bank_account_prefill,
                "payment_method": str(getattr(invoice, "payment_method", "") or "bank_transfer"),
                "footer_mode": str(getattr(invoice, "footer_mode", "") or _default_invoice_footer_mode(subject)),
                "footer_text": str(
                    getattr(invoice, "footer_text", None)
                    or _invoice_footer_text_for_mode(getattr(invoice, "footer_mode", None), subject=subject)
                ),
                "due_term": _infer_due_term_value(invoice.issue_date, invoice.due_date),
                "discount_amount": _cents_to_amount_str(getattr(invoice, "discount_cents", 0)) if int(getattr(invoice, "discount_cents", 0) or 0) != 0 else "",
                "rounding_enabled": rounding_enabled,
                "rounding_adjustment": _cents_to_amount_str(invoice.rounding_adjustment_cents) if int(invoice.rounding_adjustment_cents or 0) != 0 else ("0.00" if rounding_enabled else ""),
                "notes": invoice.notes or "",
                "variable_symbol": _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)),
                "final_number_preview": final_number_preview,
                "seller_party": seller_party_prefill,
                "buyer_party": buyer_party_prefill,
            }
            raw_prefill_items = [
                _invoice_item_prefill_from_model(item, is_vat_payer=bool(is_vat_payer), default_vat_rate=default_vat_rate) for item in items
            ]
            prefill, prefill_items = _apply_invoice_editor_summary(
                prefill=prefill,
                prefill_items=raw_prefill_items,
                is_vat_payer=bool(is_vat_payer),
                allow_negative_unit_price=document_type == "credit_note",
                min_rows=max(1, len(raw_prefill_items) or 1),
                default_vat_rate=default_vat_rate,
            )

            return templates.TemplateResponse(
                request,
                "invoices/edit.html",
                {
                    "invoice": invoice,
                    "contacts": contacts,
                    "prefill": prefill,
                    "prefill_items": prefill_items,
                    "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                    "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                    "series_options": _build_invoice_series_options(series_list, year=preview_year),
                    "account_options": _build_bank_account_options(bank_accounts_rows, current_invoice=invoice),
                    "currency_options": _build_currency_options(prefill.get("currency")),
                    "catalog_items": _list_invoice_catalog_items(db, subject_id=int(sid), currency=prefill.get("currency"), limit=12),
                    "is_vat_payer": bool(is_vat_payer),
                    "vat_rate_options": vat_rate_options,
                    "default_vat_rate": default_vat_rate,
                    "subject_country": subject_country,
                    "due_term_options": INVOICE_DUE_TERM_OPTIONS,
                    "item_unit_options": INVOICE_ITEM_UNIT_OPTIONS,
                    "payment_method_options": INVOICE_PAYMENT_METHOD_OPTIONS,
                    "invoice_language_options": INVOICE_LANGUAGE_OPTIONS,
                    "invoice_style_options": INVOICE_STYLE_OPTIONS,
                    "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                    "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                    "back_url": f"/invoices/{invoice.id}",
                    "form_action": duplicate_form_action,
                    "issue_form_action": f"/invoices/{invoice.id}/edit/issue",
                    "duplicated_mode": bool(duplicated_mode),
                    "public_url": public_urls["view"] if public_urls else None,
                    "page_title": duplicate_page_title or _invoice_page_title_for_type(document_type, mode="edit"),
                    "page_subtitle": duplicate_page_subtitle or (
                        "Nabídku můžeš průběžně ladit, pak z ní uděláš zálohovku nebo ostrou fakturu bez přepisování položek."
                        if document_type == "quote"
                        else (
                            "Dobropis navazuje na původní fakturu, drží vlastní číselnou řadu a automaticky ponechá záporné položky."
                            if document_type == "credit_note"
                            else (
                                "Zálohovou fakturu můžeš upravit stejně jako běžný doklad. V PDF i číselné řadě se drží samostatně."
                                if document_type == "proforma"
                                else (
                                    "Uprav hlavičku i položky konceptu na jedné obrazovce."
                                    if str(invoice.status or "").strip().lower() == "draft"
                                    else "Fakturu můžeš upravit i po vystavení. Finální číslo zůstává zachované a změny se zapíšou do historie."
                                )
                            )
                        )
                    ),
                    "submit_label": "Vystavit nový doklad" if duplicated_mode else "Uložit změny",
                    "series_locked": str(invoice.status or "").strip().lower() != "draft",
                },
            )

        @app.post("/invoices/{invoice_id}/edit")
        @app.post("/invoices/{invoice_id}/edit/issue")
        async def invoices_update(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            form = await request.form()
            today = date.today()
            sid = _current_subject_id()
            document_type = "invoice"
            duplicated_mode = str(request.query_params.get("duplicated") or "").strip() == "1"
            duplicated_from = str(request.query_params.get("from") or "").strip()
            success_next_url = _safe_next_url(request.query_params.get("next") or form.get("next"), f"/invoices/{int(invoice_id)}")

            def _preview_year(value: str | None, fallback_year: int) -> int:
                try:
                    return date.fromisoformat(str(value or "")).year
                except Exception:
                    return fallback_year

            def _prefill_from_form(default_bank_account: SubjectBankAccount | None) -> dict:
                try:
                    cid = int(form.get("contact_id")) if form.get("contact_id") else None
                except Exception:
                    cid = None
                try:
                    series_id = int(form.get("series_id")) if form.get("series_id") else None
                except Exception:
                    series_id = None
                bank_account_raw = None
                try:
                    if "bank_account_id" in form:
                        bank_account_raw = (form.get("bank_account_id") or "").strip()
                    elif default_bank_account is not None:
                        bank_account_raw = str(int(default_bank_account.id))
                    else:
                        bank_account_raw = ""
                except Exception:
                    bank_account_raw = ""
                return {
                    "contact_id": cid,
                    "document_type": document_type,
                    "source_invoice_id": int(getattr(invoice, "source_invoice_id", 0) or 0) or None,
                    "issue_date": (form.get("issue_date") or today.isoformat()).strip(),
                    "taxable_supply_date": (form.get("taxable_supply_date") or form.get("issue_date") or today.isoformat()).strip(),
                    "due_date": (form.get("due_date") or today.isoformat()).strip(),
                    "currency": (form.get("currency") or "CZK").strip() or "CZK",
                    "invoice_language": _normalize_invoice_language(form.get("invoice_language") or getattr(invoice, "invoice_language", None)),
                    "invoice_style": _normalize_invoice_style(form.get("invoice_style") or getattr(invoice, "invoice_style", None) or _default_invoice_style(subject)),
                    "series_id": series_id,
                    "bank_account_id": bank_account_raw,
                    "payment_method": (form.get("payment_method") or "bank_transfer").strip() or "bank_transfer",
                    "footer_mode": (form.get("footer_mode") or _default_invoice_footer_mode(subject)).strip() or _default_invoice_footer_mode(subject),
                    "footer_text": (form.get("footer_text") or "").strip(),
                    "due_term": (form.get("due_term") or "14").strip() or "14",
                    "discount_amount": (form.get("discount_amount") or "").strip(),
                    "rounding_enabled": bool(form.get("rounding_enabled")),
                    "rounding_adjustment": (form.get("rounding_adjustment") or "").strip(),
                    "notes": (form.get("notes") or "").strip(),
                    "variable_symbol": _normalize_variable_symbol(form.get("variable_symbol")),
                    "seller_party": _party_payload_from_form(
                        form,
                        prefix="seller",
                        fallback=_party_payload_from_snapshot_or_fallback(
                            db,
                            invoice_id=int(invoice.id),
                            role="seller",
                            fallback=_party_payload_from_subject(subject),
                        ),
                    ),
                    "buyer_party": _party_payload_from_form(
                        form,
                        prefix="buyer",
                        fallback=_party_payload_from_snapshot_or_fallback(
                            db,
                            invoice_id=int(invoice.id),
                            role="buyer",
                            fallback=_party_payload_from_contact(invoice.contact),
                        ),
                    ),
                }

            def _duplicate_edit_context() -> tuple[str | None, str | None, str, str]:
                if not duplicated_mode:
                    return None, None, "Uložit změny", f"/invoices/{invoice.id}/edit"
                if document_type == "quote":
                    page_title = "Nová zduplikovaná nabídka"
                    page_subtitle = "Vznikla nová kopie nabídky. Zkontroluj údaje, případně je uprav, a pak ji můžeš rovnou používat dál."
                elif document_type == "credit_note":
                    page_title = "Nový zduplikovaný dobropis"
                    page_subtitle = "Vznikla nová kopie dobropisu. Zkontroluj údaje a uprav ji podle potřeby."
                elif document_type == "proforma":
                    page_title = "Nová zduplikovaná zálohová faktura"
                    page_subtitle = "Vznikla nová kopie zálohové faktury. Zkontroluj údaje a uprav ji podle potřeby."
                else:
                    page_title = "Nová zduplikovaná faktura"
                    page_subtitle = "Vznikla nová kopie faktury. Zkontroluj údaje, případně je uprav, a pak ji vystav jako nový doklad."
                if duplicated_from:
                    page_subtitle += f" Vychází z dokladu {duplicated_from}."
                form_action = f"/invoices/{invoice.id}/edit/issue?duplicated=1"
                if duplicated_from:
                    form_action += f"&from={quote(duplicated_from, safe='')}"
                return page_title, page_subtitle, "Vystavit nový doklad", form_action

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                    .where(Invoice.contact.has(Contact.subject_id == sid))
                    .options(selectinload(Invoice.contact))
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})
            document_type = _normalize_invoice_document_type(getattr(invoice, "document_type", "invoice"))

            try:
                existing_items = db.scalars(
                    select(InvoiceItem)
                    .where(InvoiceItem.invoice_id == int(invoice_id))
                    .order_by(InvoiceItem.sort_order.asc(), InvoiceItem.id.asc())
                ).all()
                contacts = db.scalars(
                    select(Contact)
                    .where(Contact.subject_id == sid)
                    .order_by(Contact.name.asc())
                ).all()
                default_series = _get_or_create_default_invoice_series(
                    db,
                    subject_id=sid,
                    document_type=document_type,
                )
                series_list = db.scalars(
                    select(InvoiceSeries)
                    .where(InvoiceSeries.subject_id == sid)
                    .order_by(InvoiceSeries.name.asc())
                ).all()
                bank_accounts_rows = _list_subject_bank_accounts(db, subject_id=sid)
                default_bank_account = _default_subject_bank_account(
                    db,
                    subject_id=sid,
                    currency=(form.get("currency") or getattr(invoice, "currency", None) or "CZK"),
                )
                subject = _load_subject_for_current_session(db)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))

            is_vat_payer, _default_currency = _subject_flags(db)
            subject_country = _invoice_subject_country(subject)
            vat_rate_options = _invoice_vat_rate_options(subject_country)
            default_vat_rate = _invoice_default_vat_rate(subject_country)
            prefill = _prefill_from_form(default_bank_account)
            status_key = str(invoice.status or "").strip().lower()
            if status_key != "draft":
                prefill["series_id"] = int(invoice.series_id) if invoice.series_id else None
            preview_year = _preview_year(prefill.get("issue_date"), invoice.issue_date.year)
            _sync_series_list_for_year(
                db,
                subject_id=sid,
                series_list=series_list,
                year=preview_year,
            )
            if status_key == "draft":
                prefill["final_number_preview"] = _series_next_number_preview(
                    _pick_invoice_series_for_preview(
                        series_list,
                        selected_id=prefill.get("series_id"),
                        default_series=default_series,
                    ),
                    year=preview_year,
                )
            else:
                prefill["final_number_preview"] = invoice.number
            raw_prefill_items: list[dict[str, str]] = []

            def _render_edit_editor(*, error: str, status_code: int = 400):
                duplicate_page_title, duplicate_page_subtitle, duplicate_submit_label, duplicate_form_action = _duplicate_edit_context()
                editor_prefill = dict(prefill)
                year = _preview_year(editor_prefill.get("issue_date"), invoice.issue_date.year)
                if status_key == "draft":
                    editor_prefill["final_number_preview"] = _series_next_number_preview(
                        _pick_invoice_series_for_preview(
                            series_list,
                            selected_id=editor_prefill.get("series_id"),
                            default_series=default_series,
                        ),
                        year=year,
                    )
                else:
                    editor_prefill["final_number_preview"] = invoice.number
                editor_prefill, editor_items = _apply_invoice_editor_summary(
                    prefill=editor_prefill,
                    prefill_items=raw_prefill_items,
                    is_vat_payer=bool(is_vat_payer),
                    allow_negative_unit_price=document_type == "credit_note",
                    min_rows=max(1, len(raw_prefill_items) or 1),
                    default_vat_rate=default_vat_rate,
                )
                return templates.TemplateResponse(
                    request,
                    "invoices/edit.html",
                    {
                        "invoice": invoice,
                        "contacts": contacts,
                        "prefill": editor_prefill,
                        "prefill_items": editor_items,
                        "setup_warnings": _subject_setup_warnings(db, subject=subject, require_bank_account=True),
                        "issued_pdf_refresh_count": _count_refreshable_issued_invoices(db, subject_id=int(sid)),
                        "series_options": _build_invoice_series_options(series_list, year=year),
                        "account_options": _build_bank_account_options(bank_accounts_rows, current_invoice=invoice),
                        "currency_options": _build_currency_options(editor_prefill.get("currency")),
                        "catalog_items": _list_invoice_catalog_items(db, subject_id=int(sid), currency=editor_prefill.get("currency"), limit=12),
                        "is_vat_payer": bool(is_vat_payer),
                        "vat_rate_options": vat_rate_options,
                        "default_vat_rate": default_vat_rate,
                        "subject_country": subject_country,
                        "due_term_options": INVOICE_DUE_TERM_OPTIONS,
                        "item_unit_options": INVOICE_ITEM_UNIT_OPTIONS,
                        "payment_method_options": INVOICE_PAYMENT_METHOD_OPTIONS,
                        "invoice_language_options": INVOICE_LANGUAGE_OPTIONS,
                        "invoice_style_options": INVOICE_STYLE_OPTIONS,
                        "footer_preset_options": INVOICE_FOOTER_PRESET_OPTIONS,
                        "footer_preset_map": INVOICE_FOOTER_PRESET_TEXTS,
                        "error": error,
                        "back_url": f"/invoices/{invoice.id}",
                        "issue_form_action": f"/invoices/{invoice.id}/edit/issue",
                        "duplicated_mode": bool(duplicated_mode),
                        "page_title": duplicate_page_title or _invoice_page_title_for_type(document_type, mode="edit"),
                        "page_subtitle": duplicate_page_subtitle or (
                            "Nabídku můžeš průběžně ladit, pak z ní uděláš zálohovku nebo ostrou fakturu bez přepisování položek."
                            if document_type == "quote"
                            else (
                                "Dobropis navazuje na původní fakturu, drží vlastní číselnou řadu a automaticky ponechá záporné položky."
                                if document_type == "credit_note"
                                else (
                                    "Zálohovou fakturu můžeš upravit stejně jako běžný doklad. V PDF i číselné řadě se drží samostatně."
                                    if document_type == "proforma"
                                    else (
                                        "Uprav hlavičku i položky konceptu na jedné obrazovce."
                                        if status_key == "draft"
                                        else "Fakturu můžeš upravit i po vystavení. Finální číslo zůstává zachované a změny se zapíšou do historie."
                                    )
                                )
                            )
                        ),
                        "submit_label": duplicate_submit_label,
                        "form_action": duplicate_form_action,
                        "series_locked": status_key != "draft",
                    },
                    status_code=status_code,
                )

            try:
                items_payload, raw_prefill_items = _parse_invoice_items_from_form(
                    form,
                    is_vat_payer=bool(is_vat_payer),
                    allow_negative_unit_price=document_type == "credit_note",
                    default_vat_rate=default_vat_rate,
                )
            except ValueError as exc:
                return _render_edit_editor(error=str(exc))

            if status_key == "draft" and not prefill.get("series_id") and default_series is not None:
                prefill["series_id"] = int(default_series.id)

            if not prefill["contact_id"]:
                return _render_edit_editor(error="Vyber odběratele.")

            try:
                issue_date = date.fromisoformat(prefill["issue_date"])
                taxable_supply_date = date.fromisoformat(prefill.get("taxable_supply_date") or prefill["issue_date"])
                due_date = date.fromisoformat(prefill["due_date"])
            except ValueError:
                return _render_edit_editor(error="Špatný formát data.")

            prefill["taxable_supply_date"] = taxable_supply_date.isoformat()
            prefill["due_term"] = _infer_due_term_value(issue_date, due_date)

            items_total_cents = sum(int(item.get("line_total_cents") or 0) for item in items_payload)

            try:
                discount_cents = parse_money_to_cents(prefill.get("discount_amount"))
            except ValueError as exc:
                return _render_edit_editor(error=str(exc))
            if discount_cents > max(items_total_cents, 0):
                return _render_edit_editor(error="Sleva nesmí být vyšší než mezisoučet.")

            try:
                rounding_adj_cents = parse_money_to_signed_cents(prefill["rounding_adjustment"])
            except ValueError as exc:
                return _render_edit_editor(error=str(exc))
            if bool(prefill.get("rounding_enabled")):
                rounding_adj_cents = compute_rounding_adjustment_cents(items_total_cents - discount_cents)
                prefill["rounding_adjustment"] = _cents_to_amount_str(rounding_adj_cents) if rounding_adj_cents != 0 else "0.00"

            try:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.id == int(prefill["contact_id"]))
                    .where(Contact.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
            if contact is None:
                return _render_edit_editor(error="Kontakt neexistuje.")
            if not _party_payload_has_meaningful_values(prefill.get("buyer_party")):
                prefill["buyer_party"] = _normalize_party_payload(_party_payload_from_contact(contact))

            if document_type == "credit_note" and prefill.get("source_invoice_id") is not None:
                try:
                    source_invoice = db.scalar(
                        select(Invoice)
                        .where(Invoice.id == int(prefill["source_invoice_id"]))
                        .where(Invoice.subject_id == int(sid))
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
                if source_invoice is None:
                    return _render_edit_editor(error="Původní fakturu pro dobropis se nepodařilo najít.")
                available_credit_cents = _credit_note_available_cents(
                    db,
                    source_invoice=source_invoice,
                    current_credit_note_id=int(invoice.id),
                )
                proposed_credit_cents = abs(int(items_total_cents - discount_cents + rounding_adj_cents))
                if proposed_credit_cents > available_credit_cents:
                    return _render_edit_editor(
                        error=f"Dobropisem už bys překročil částku původní faktury. Zbývá dobropisovat maximálně {format_cents(available_credit_cents, str(source_invoice.currency or 'CZK'))}."
                    )

            selected_series: InvoiceSeries | None = None
            if status_key == "draft" and prefill.get("series_id"):
                try:
                    selected_series = db.scalar(
                        select(InvoiceSeries)
                        .where(InvoiceSeries.id == int(prefill["series_id"]))
                        .where(InvoiceSeries.subject_id == sid)
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
            if status_key == "draft" and selected_series is None:
                selected_series = default_series

            selected_bank_account: SubjectBankAccount | None = None
            bank_account_raw = str(prefill.get("bank_account_id") or "").strip()
            preserve_snapshot = bank_account_raw == "snapshot"
            if bank_account_raw and bank_account_raw != "snapshot":
                if not bank_account_raw.isdigit():
                    return _render_edit_editor(error="Vybraný účet neexistuje.")
                try:
                    selected_bank_account = db.scalar(
                        select(SubjectBankAccount)
                        .where(SubjectBankAccount.id == int(bank_account_raw))
                        .where(SubjectBankAccount.subject_id == sid)
                    )
                except SQLAlchemyError as exc:  # type: ignore[misc]
                    return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
                if selected_bank_account is None:
                    return _render_edit_editor(error="Vybraný účet neexistuje.")

            payment_method = str(prefill.get("payment_method") or "bank_transfer").strip().lower() or "bank_transfer"
            if payment_method not in {value for value, _label in INVOICE_PAYMENT_METHOD_OPTIONS}:
                return _render_edit_editor(error="Neplatný způsob platby.")
            footer_mode, footer_text = _resolve_invoice_footer(
                subject=subject,
                footer_mode=prefill.get("footer_mode"),
                footer_text=prefill.get("footer_text"),
                language=prefill.get("invoice_language"),
            )

            before_items_signature = [
                (
                    str(getattr(item, "description", "") or ""),
                    str(getattr(item, "quantity", "") or ""),
                    int(getattr(item, "unit_price_cents", 0) or 0),
                    str(getattr(item, "vat_rate", "0") or "0"),
                )
                for item in existing_items
            ]
            after_items_signature = [
                (
                    str(item.get("description") or ""),
                    str(item.get("quantity") or ""),
                    int(item.get("unit_price_cents") or 0),
                    str(item.get("vat_rate") or "0"),
                )
                for item in items_payload
            ]
            before_total_cents = int(invoice.total_cents or 0)
            before_state = {
                "contact_id": int(invoice.contact_id),
                "issue_date": str(invoice.issue_date),
                "taxable_supply_date": str(_invoice_taxable_supply_date(invoice) or invoice.issue_date),
                "due_date": str(invoice.due_date),
                "currency": str(invoice.currency or ""),
                "notes": str(invoice.notes or ""),
                "variable_symbol": _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)),
                "payment_method": str(getattr(invoice, "payment_method", "") or "bank_transfer"),
                "footer_mode": str(getattr(invoice, "footer_mode", "") or ""),
                "footer_text": str(getattr(invoice, "footer_text", "") or ""),
                "discount_cents": int(getattr(invoice, "discount_cents", 0) or 0),
                "rounding_adjustment_cents": int(invoice.rounding_adjustment_cents or 0),
                "series_id": int(invoice.series_id) if invoice.series_id is not None else None,
                "bank_account_id": int(invoice.bank_account_id) if invoice.bank_account_id is not None else None,
                "bank_account_number": str(invoice.bank_account_number or ""),
                "bank_account_iban": str(invoice.bank_account_iban or ""),
            }

            invoice.contact_id = contact.id
            invoice.issue_date = issue_date
            invoice.taxable_supply_date = taxable_supply_date
            invoice.due_date = due_date
            invoice.currency = prefill["currency"].upper()
            invoice.invoice_language = _normalize_invoice_language(prefill.get("invoice_language"))
            invoice.invoice_style = _normalize_invoice_style(prefill.get("invoice_style"))
            invoice.notes = prefill["notes"] or None
            invoice.variable_symbol = (
                prefill.get("variable_symbol")
                or _contact_fixed_variable_symbol(contact)
                or (variable_symbol_from_invoice_number(invoice.number) if status_key != "draft" and str(getattr(invoice, "number", "") or "").strip() else None)
            )
            invoice.payment_method = payment_method
            invoice.footer_mode = footer_mode
            invoice.footer_text = footer_text or None
            invoice.discount_cents = int(discount_cents)
            invoice.rounding_adjustment_cents = int(rounding_adj_cents)
            if status_key == "draft":
                invoice.series_id = int(selected_series.id) if selected_series is not None else None

            issue_after_save = bool(
                status_key == "draft"
                and (
                    duplicated_mode
                    or str(request.url.path or "").rstrip("/").endswith("/edit/issue")
                )
            )
            if issue_after_save and settings.auth_required:
                user_id = request.session.get("user_id")
                if not user_id:
                    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
                issue_link = db.scalar(
                    select(UserSubject).where(
                        UserSubject.user_id == int(user_id),
                        UserSubject.subject_id == int(sid),
                    )
                )
                if issue_link is None or not bool(getattr(issue_link, "can_issue", False)):
                    return JSONResponse(status_code=403, content={"detail": "Access denied"})
            previous_contact_id = int(before_state.get("contact_id") or 0)
            selected_contact_id = int(getattr(contact, "id", 0) or 0)
            sync_party_snapshots = status_key == "draft" or selected_contact_id != previous_contact_id

            try:
                _sync_invoice_parties(
                    db,
                    invoice=invoice,
                    subject=subject,
                    contact=contact,
                    sync_existing=sync_party_snapshots,
                )
                _apply_manual_invoice_parties(
                    db,
                    invoice=invoice,
                    buyer_payload=prefill.get("buyer_party") or _party_payload_from_contact(contact),
                )

                if preserve_snapshot:
                    invoice.bank_account_id = None
                elif selected_bank_account is not None:
                    _apply_invoice_bank_account_snapshot(
                        invoice,
                        account=selected_bank_account,
                        subject=subject,
                        allow_subject_fallback=True,
                    )
                else:
                    invoice.bank_account_id = None
                    invoice.bank_account_label = None
                    invoice.bank_account_number = None
                    invoice.bank_account_iban = None
                    invoice.bank_account_bic = None
                    invoice.bank_account_country = None

                _replace_invoice_items(
                    db,
                    invoice_id=int(invoice.id),
                    items_payload=items_payload,
                )

                _recalc_invoice_total_cents(db, invoice=invoice)

                if issue_after_save:
                    if selected_series is None:
                        selected_series = default_series
                    if selected_series is None:
                        raise ValueError("Nepodařilo se připravit číselnou řadu.")
                    invoice.series_id = int(selected_series.id)
                    issued_number = _allocate_next_invoice_number(
                        db,
                        subject_id=int(sid),
                        series_id=int(selected_series.id),
                        invoice_id=int(invoice.id),
                        issue_date=invoice.issue_date,
                    )
                    invoice.number = issued_number
                    if not _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)):
                        invoice.variable_symbol = _contact_fixed_variable_symbol(contact) or variable_symbol_from_invoice_number(issued_number)
                    invoice.status = "issued"
                    invoice.issued_at = utc_now()
                    invoice.sent_at = None
                    invoice.paid_on = None
                    if subject is not None:
                        _maybe_ensure_invoice_public_link(db, invoice=invoice, subject=subject)

                changed_fields: list[str] = []
                after_state = {
                    "contact_id": int(invoice.contact_id),
                    "issue_date": str(invoice.issue_date),
                    "due_date": str(invoice.due_date),
                    "currency": str(invoice.currency or ""),
                    "notes": str(invoice.notes or ""),
                    "variable_symbol": _normalize_variable_symbol(getattr(invoice, "variable_symbol", None)),
                    "payment_method": str(getattr(invoice, "payment_method", "") or "bank_transfer"),
                    "footer_mode": str(getattr(invoice, "footer_mode", "") or ""),
                    "footer_text": str(getattr(invoice, "footer_text", "") or ""),
                    "discount_cents": int(getattr(invoice, "discount_cents", 0) or 0),
                    "rounding_adjustment_cents": int(invoice.rounding_adjustment_cents or 0),
                    "series_id": int(invoice.series_id) if invoice.series_id is not None else None,
                    "bank_account_id": int(invoice.bank_account_id) if invoice.bank_account_id is not None else None,
                    "bank_account_number": str(invoice.bank_account_number or ""),
                    "bank_account_iban": str(invoice.bank_account_iban or ""),
                }
                for key, before_value in before_state.items():
                    if before_value != after_state.get(key):
                        changed_fields.append(key)
                items_changed = before_items_signature != after_items_signature

                _audit_log(
                    db,
                    action=(
                        "invoice_duplicate_issued"
                        if issue_after_save and duplicated_mode
                        else (
                            "invoice_issued"
                            if issue_after_save
                            else ("invoice_duplicate_updated" if duplicated_mode else "invoice_updated")
                        )
                    ),
                    entity_type="invoice",
                    entity_id=int(invoice.id),
                    data={
                        "number": str(invoice.number or ""),
                        "changed_fields": changed_fields,
                        "items_changed": bool(items_changed),
                        "total_before_cents": before_total_cents,
                        "total_after_cents": int(invoice.total_cents or 0),
                        "currency": str(invoice.currency or "CZK"),
                        "copied_from_number": duplicated_from or None,
                    },
                    subject_id=int(sid),
                )

                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=INVOICE_EDIT_TITLE, db_error=str(exc))
            except ValueError as exc:
                db.rollback()
                return _render_edit_editor(error=str(exc))

            if status_key != "draft" or issue_after_save:
                _regenerate_invoice_pdf_best_effort(request, db, invoice_id=int(invoice.id), subject_id=int(sid))

            return RedirectResponse(url=success_next_url, status_code=303)

        @app.post("/invoices/{invoice_id}/delete")
        def invoices_delete(invoice_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            payment_rows = db.scalars(select(Payment).where(Payment.invoice_id == int(invoice.id))).all()
            payment_ids = {int(getattr(payment, "id", 0) or 0) for payment in payment_rows if getattr(payment, "id", None) is not None}
            matched_bank_rows = {
                int(getattr(row, "id", 0)): row
                for row in db.scalars(
                    select(BankTransaction).where(BankTransaction.matched_invoice_id == int(invoice.id))
                ).all()
            }
            if payment_ids:
                for row in db.scalars(
                    select(BankTransaction).where(BankTransaction.payment_id.in_(payment_ids))
                ).all():
                    matched_bank_rows[int(getattr(row, "id", 0) or 0)] = row

            try:
                for linked_invoice in db.scalars(
                    select(Invoice).where(Invoice.source_invoice_id == int(invoice.id))
                ).all():
                    linked_invoice.source_invoice_id = None

                for plan in db.scalars(
                    select(RecurringInvoicePlan).where(RecurringInvoicePlan.template_invoice_id == int(invoice.id))
                ).all():
                    db.delete(plan)

                for plan in db.scalars(
                    select(RecurringInvoicePlan).where(RecurringInvoicePlan.last_generated_invoice_id == int(invoice.id))
                ).all():
                    plan.last_generated_invoice_id = None

                for row in matched_bank_rows.values():
                    if int(getattr(row, "matched_invoice_id", 0) or 0) == int(invoice.id):
                        row.matched_invoice_id = None
                    if int(getattr(row, "payment_id", 0) or 0) in payment_ids:
                        row.payment_id = None
                    row.matched_at = None

                for payment in payment_rows:
                    db.delete(payment)

                _audit_log(
                    db,
                    action="invoice_deleted",
                    entity_type="invoice",
                    entity_id=int(invoice.id),
                    data={
                        "number": str(getattr(invoice, "number", "") or ""),
                        "status": str(getattr(invoice, "status", "") or ""),
                    },
                    subject_id=int(sid),
                )
                db.delete(invoice)
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title=INVOICE_TITLE, db_error=str(exc))

            return RedirectResponse(url="/invoices", status_code=303)

        @app.post("/invoices/{invoice_id}/items/new")
        async def invoice_items_create(
            invoice_id: int,
            request: Request,
            db: Session = Depends(get_db),
        ):
            form = await request.form()

            sid = _current_subject_id()

            subject = _load_subject_for_current_session(db)
            is_vat_payer, _default_currency = _subject_flags(db, subject_override=subject)
            default_vat_rate = _invoice_default_vat_rate(_invoice_subject_country(subject))

            description = (form.get("description") or "").strip()
            prefill_item = {
                "description": description,
                "quantity": (form.get("quantity") or "1.00").strip(),
                "unit_price": (form.get("unit_price") or "0.00").strip(),
                "vat_rate": (form.get("vat_rate") or (default_vat_rate if is_vat_payer else "0")).strip(),
            }

            if not description:
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Popis položky je povinný.",
                    prefill_item=prefill_item,
                    status_code=400,
                )

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Faktura", db_error=str(exc))
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            try:
                quantity = parse_quantity(form.get("quantity"))
                unit_price_cents = parse_money_to_cents(form.get("unit_price"))
                is_vat_payer, _default_currency = _subject_flags(db, subject_override=subject)
                if is_vat_payer:
                    vat_rate = parse_vat_rate(prefill_item.get("vat_rate"))
                else:
                    vat_rate = Decimal("0.00")
            except ValueError as exc:
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error=str(exc),
                    prefill_item=prefill_item,
                    status_code=400,
                )

            try:
                max_sort = db.scalar(
                    select(func.coalesce(func.max(InvoiceItem.sort_order), 0)).where(
                        InvoiceItem.invoice_id == invoice_id
                    )
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return _render_db_disabled(request, title="Faktura", db_error=str(exc))
            sort_order = int(max_sort or 0) + 1

            line_net_cents, line_vat_cents, line_total_cents = compute_line_amounts_cents(
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                vat_rate=vat_rate,
            )

            item = InvoiceItem(
                invoice_id=invoice_id,
                description=description,
                quantity=quantity,
                unit_price_cents=unit_price_cents,
                vat_rate=vat_rate,
                line_net_cents=line_net_cents,
                line_vat_cents=line_vat_cents,
                line_total_cents=line_total_cents,
                sort_order=sort_order,
            )
            db.add(item)
            try:
                db.flush()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Faktura", db_error=str(exc))

            try:
                _recalc_invoice_total_cents(db, invoice=invoice)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Faktura", db_error=str(exc))

            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return _render_db_disabled(request, title="Faktura", db_error=str(exc))

            return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

        @app.post("/invoices/{invoice_id}/items/{item_id}/delete")
        def invoice_items_delete(invoice_id: int, item_id: int, request: Request, db: Session = Depends(get_db)):
            sid = _current_subject_id()
            try:
                item = db.get(InvoiceItem, item_id)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return JSONResponse(status_code=503, content={"detail": _safe_db_error_message(exc)})
            if item is None or item.invoice_id != invoice_id:
                return JSONResponse(status_code=404, content={"detail": "Item not found"})

            try:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .where(Invoice.subject_id == sid)
                )
            except SQLAlchemyError as exc:  # type: ignore[misc]
                return JSONResponse(status_code=503, content={"detail": _safe_db_error_message(exc)})
            if invoice is None:
                return JSONResponse(status_code=404, content={"detail": "Invoice not found"})

            if str(invoice.status or "") != "draft":
                return _render_invoice_detail(
                    request=request,
                    db=db,
                    invoice_id=invoice_id,
                    error="Položky lze upravovat jen u faktury v draftu.",
                    status_code=400,
                )

            db.delete(item)
            try:
                db.flush()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=503, content={"detail": _safe_db_error_message(exc)})

            try:
                _recalc_invoice_total_cents(db, invoice=invoice)
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=503, content={"detail": _safe_db_error_message(exc)})
            try:
                db.commit()
            except SQLAlchemyError as exc:  # type: ignore[misc]
                db.rollback()
                return JSONResponse(status_code=503, content={"detail": _safe_db_error_message(exc)})

            return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=303)

    else:
        # DB dependencies are missing (common in minimal CI sandboxes). Keep the
        # UI navigable by providing placeholder pages for the main sections.

        @app.get("/contacts", response_class=HTMLResponse)
        def contacts_list_disabled(request: Request):
            return _render_db_disabled(request, title="Kontakty")

        @app.get("/contacts/export.csv")
        def contacts_export_disabled(request: Request):
            return _render_db_disabled(request, title="Export kontaktů")

        @app.get("/contacts/new", response_class=HTMLResponse)
        def contacts_new_disabled(request: Request):
            return _render_db_disabled(request, title="Nový kontakt")

        @app.get("/contacts/{contact_id}", response_class=HTMLResponse)
        def contacts_detail_disabled(contact_id: int, request: Request):
            return _render_db_disabled(request, title="Kontakt")

        @app.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
        def contacts_edit_disabled(contact_id: int, request: Request):
            return _render_db_disabled(request, title="Upravit kontakt")

        @app.get("/invoices", response_class=HTMLResponse)
        def invoices_list_disabled(request: Request):
            return _render_db_disabled(request, title="Faktury")

        @app.get("/invoices/export.csv")
        def invoices_export_disabled(request: Request):
            return _render_db_disabled(request, title="Export faktur")

        @app.get("/invoices/new", response_class=HTMLResponse)
        def invoices_new_disabled(request: Request):
            return _render_db_disabled(request, title="Nová faktura")

        @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
        def invoices_detail_disabled(invoice_id: int, request: Request):
            return _render_db_disabled(request, title="Faktura")

        @app.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
        def invoices_print_disabled(invoice_id: int, request: Request):
            return _render_db_disabled(request, title="Tisk faktury")

        @app.get("/invoices/{invoice_id}/pdf")
        def invoices_pdf_disabled(invoice_id: int, request: Request):
            # Placeholder PDF route when DB stack is missing.

            try:
                pdf_bytes = render_error_pdf_bytes(
                    title="PDF faktury",
                    message="DB je vypnutá / nedostupná – nelze vygenerovat fakturu.",
                    request_path=str(request.url.path),
                )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition_inline(f"invoice-{invoice_id}")},
                )
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

        # Phase-21: public invoice URLs are still routable, but DB is required.
        @app.get("/i/{short_code}/{invoice_number}/pdf")
        def public_invoice_pdf_short_readable_disabled(
            short_code: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
        ):
            try:
                pdf_bytes = render_error_pdf_bytes(
                    title="PDF faktury",
                    message="DB je vypnutá / nedostupná – nelze načíst veřejnou fakturu.",
                    request_path=str(request.url.path),
                )
                disp = (
                    content_disposition_attachment("invoice")
                    if bool(download)
                    else content_disposition_inline("invoice")
                )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp},
                )
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

        @app.get("/i/{short_code}/pdf")
        def public_invoice_pdf_short_disabled(
            short_code: str,
            request: Request,
            download: bool = False,
        ):
            try:
                pdf_bytes = render_error_pdf_bytes(
                    title="PDF faktury",
                    message="DB je vypnutá / nedostupná – nelze načíst veřejnou fakturu.",
                    request_path=str(request.url.path),
                )
                disp = (
                    content_disposition_attachment("invoice")
                    if bool(download)
                    else content_disposition_inline("invoice")
                )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp},
                )
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

        @app.get("/i/{short_code}/{invoice_number}", response_class=HTMLResponse)
        def public_invoice_view_short_readable_disabled(
            short_code: str,
            invoice_number: str,
            request: Request,
        ):
            return _render_db_disabled(request, title="Veřejná faktura")

        @app.get("/i/{short_code}", response_class=HTMLResponse)
        def public_invoice_view_short_disabled(
            short_code: str,
            request: Request,
        ):
            return _render_db_disabled(request, title="Veřejná faktura")

        @app.get("/{public_username}/i/{token}/{invoice_number}", response_class=HTMLResponse)
        def public_invoice_view_disabled(
            public_username: str,
            token: str,
            invoice_number: str,
            request: Request,
        ):
            return _render_db_disabled(request, title="Veřejná faktura")

        @app.get("/{public_username}/i/{token}/{invoice_number}/pdf")
        def public_invoice_pdf_disabled(
            public_username: str,
            token: str,
            invoice_number: str,
            request: Request,
            download: bool = False,
        ):
            # Placeholder PDF route when DB stack is missing.
            try:
                pdf_bytes = render_error_pdf_bytes(
                    title="PDF faktury",
                    message="DB je vypnutá / nedostupná – nelze načíst veřejnou fakturu.",
                    request_path=str(request.url.path),
                )
                disp = (
                    content_disposition_attachment("invoice")
                    if bool(download)
                    else content_disposition_inline("invoice")
                )
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": disp},
                )
            except Exception as exc:  # pragma: no cover
                return _render_db_disabled(request, title="PDF faktury", db_error=str(exc))

        @app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
        def invoices_edit_disabled(invoice_id: int, request: Request):
            return _render_db_disabled(request, title="Upravit fakturu")

        @app.post("/invoices/{invoice_id}/status")
        async def invoices_set_status_disabled(invoice_id: int, request: Request):
            return _render_db_disabled(request, title="Faktura")

    register_optional_extensions(
        app,
        settings=settings,
        templates=templates,
        project_root=project_root,
    )

    return app


app = create_app()

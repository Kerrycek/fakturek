from __future__ import annotations

"""Helpers for public invoice links.

These helpers are intentionally small and dependency-light at import time so they
can be used from both the FastAPI app and standalone import utilities.
"""

from urllib.parse import quote, urlsplit, urlunsplit
import base64
import hashlib
import hmac
import re
import secrets

PUBLIC_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_LOCAL_PUBLIC_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def slugify_public_username(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"[^a-z0-9]+", "-", v)
    v = re.sub(r"-+", "-", v).strip("-")
    return (v[:64]).strip("-")


def ensure_subject_public_username(db, *, subject) -> str:
    """Ensure ``subject.public_username`` is set to a valid unique value."""

    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    current = (getattr(subject, "public_username", None) or "").strip().lower()
    if current and PUBLIC_USERNAME_RE.match(current):
        return current

    base = slugify_public_username(getattr(subject, "name", "") or "")
    if not base or len(base) < 3:
        base = f"s{int(subject.id)}"
    base = base[:64]

    for i in range(0, 50):
        if i == 0:
            candidate = base
        else:
            suffix = f"-{i+1}"
            candidate = (base[: max(0, 64 - len(suffix))] + suffix).strip("-")

        try:
            exists = db.scalar(
                select(type(subject).id)
                .where(type(subject).public_username == candidate)
                .where(type(subject).id != int(subject.id))
            )
        except SQLAlchemyError:
            exists = None

        if exists is None and PUBLIC_USERNAME_RE.match(candidate):
            subject.public_username = candidate
            db.add(subject)
            return candidate

    fallback = f"s{int(subject.id)}"
    subject.public_username = fallback
    db.add(subject)
    return fallback


def generate_unique_invoice_public_token(db) -> str:
    """Generate a unique random token for ``Invoice.public_token``."""

    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError
    from fakturek.models import Invoice

    for _ in range(0, 20):
        token = secrets.token_urlsafe(32).strip().replace("=", "")
        try:
            exists = db.scalar(select(Invoice.id).where(Invoice.public_token == token))
        except SQLAlchemyError:
            exists = None
        if exists is None and token:
            return token
    raise RuntimeError("Failed to allocate unique public token")


def _base36_encode(number: int) -> str:
    if number < 0:
        raise ValueError("number must be >= 0")
    if number == 0:
        return "0"
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    value = int(number)
    while value:
        value, rem = divmod(value, 36)
        out.append(alphabet[rem])
    return "".join(reversed(out))


def build_public_invoice_short_code(*, invoice_id: int, token: str, secret_key: str) -> str:
    """Build a compact, deterministic public share code.

    The short code is intentionally derived from the stored public token so it
    becomes invalid when the token is rotated/disabled, while being much shorter
    than exposing the whole token in the URL.
    """

    iid = int(invoice_id or 0)
    tok = str(token or "").strip()
    secret = str(secret_key or "").strip()
    if iid <= 0 or not tok or not secret:
        raise ValueError("invoice_id, token and secret_key are required")

    payload = f"invoice:{iid}:{tok}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:9]
    sig = base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
    return f"{_base36_encode(iid)}-{sig}"


def parse_public_invoice_short_code(short_code: str) -> tuple[int, str] | None:
    raw = (short_code or "").strip()
    if not raw or "-" not in raw:
        return None
    id_part, sig = raw.split("-", 1)
    if not id_part or not sig or not re.fullmatch(r"[0-9a-z]+", id_part):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", sig):
        return None
    try:
        invoice_id = int(id_part, 36)
    except ValueError:
        return None
    if invoice_id <= 0:
        return None
    return invoice_id, sig


def verify_public_invoice_short_code(*, short_code: str, invoice_id: int, token: str, secret_key: str) -> bool:
    parsed = parse_public_invoice_short_code(short_code)
    if parsed is None:
        return False
    parsed_invoice_id, parsed_sig = parsed
    if parsed_invoice_id != int(invoice_id or 0):
        return False
    expected = build_public_invoice_short_code(
        invoice_id=int(invoice_id),
        token=str(token or ""),
        secret_key=str(secret_key or ""),
    )
    expected_sig = expected.split("-", 1)[1]
    return hmac.compare_digest(parsed_sig, expected_sig)


def ensure_invoice_public_link(db, *, invoice, subject=None) -> dict[str, str | bool | None]:
    """Best-effort ensure subject username + invoice token exist.

    Returns a tiny status payload that can be used in import summaries.
    """

    from sqlalchemy import select
    from fakturek.models import Subject

    subject_row = subject
    if subject_row is None:
        subject_id = int(getattr(invoice, "subject_id", 0) or 0)
        if subject_id:
            subject_row = db.scalar(select(Subject).where(Subject.id == subject_id).limit(1))

    username = None
    if subject_row is not None:
        username = ensure_subject_public_username(db, subject=subject_row)

    token_before = (getattr(invoice, "public_token", None) or "").strip()
    if not token_before:
        invoice.public_token = generate_unique_invoice_public_token(db)
        db.add(invoice)
        token_before = str(invoice.public_token or "")

    return {
        "public_username": username,
        "public_token": token_before or None,
        "created_token": bool((getattr(invoice, "public_token", None) or "").strip()),
    }


def build_public_invoice_relative_path(*, public_username: str, token: str, invoice_number: str) -> str:
    inv_no = quote(str(invoice_number or ""), safe="")
    return f"/{str(public_username or '').strip().lower()}/i/{str(token or '').strip()}/{inv_no}"


def slugify_public_invoice_number(value: str) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return (raw[:96]).strip("-") or "invoice"


def build_public_invoice_short_relative_path(*, short_code: str, invoice_number: str) -> str:
    code = quote(str(short_code or "").strip(), safe="")
    label = quote(slugify_public_invoice_number(invoice_number), safe="")
    if not code:
        return "/i"
    if label:
        return f"/i/{code}/{label}"
    return f"/i/{code}"


def _request_client_host(request) -> str:
    try:
        return str(request.client.host if request and request.client else "").strip()
    except Exception:
        return ""



def resolve_public_base_url(
    *,
    request=None,
    configured_base_url: str | None = None,
    trusted_proxy_ips: tuple[str, ...] | list[str] | set[str] | None = None,
    allow_host_header_fallback: bool = True,
) -> str | None:
    configured = (configured_base_url or "").strip().rstrip("/")
    if configured:
        return configured

    if request is None:
        return None

    headers = getattr(request, "headers", None)
    scheme = ""
    host = ""

    trusted_proxies = {
        str(item or "").strip().lower()
        for item in (trusted_proxy_ips or ())
        if str(item or "").strip()
    }
    direct_host = _request_client_host(request).lower()
    trust_forwarded_headers = bool(trusted_proxies) and direct_host in trusted_proxies

    if headers is not None and trust_forwarded_headers:
        scheme = (headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
        host = (headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()

    if not host and allow_host_header_fallback and headers is not None:
        host = (headers.get("host") or "").split(",", 1)[0].strip()

    if not scheme:
        try:
            scheme = str(request.url.scheme or "")
        except Exception:
            scheme = ""

    scheme = scheme.lower() or "https"
    if scheme not in {"http", "https"}:
        scheme = "https"
    if not host:
        return None

    parsed = urlsplit(f"//{host}")
    hostname = (parsed.hostname or "").strip().lower()
    port = parsed.port
    if not hostname:
        return None

    host_display = hostname
    if ":" in hostname and not hostname.startswith("["):
        host_display = f"[{hostname}]"

    if hostname in _LOCAL_PUBLIC_HOSTS and port:
        host_display = f"{host_display}:{int(port)}"

    return urlunsplit((scheme, host_display, "", "", "")).rstrip("/")


def build_public_invoice_urls(
    *,
    public_username: str,
    token: str,
    invoice_number: str,
    base_url: str | None = None,
    invoice_id: int | None = None,
    secret_key: str | None = None,
) -> dict[str, str]:
    legacy_path = build_public_invoice_relative_path(
        public_username=public_username,
        token=token,
        invoice_number=invoice_number,
    )
    short_code: str | None = None
    if int(invoice_id or 0) > 0 and (secret_key or "").strip():
        try:
            short_code = build_public_invoice_short_code(
                invoice_id=int(invoice_id),
                token=token,
                secret_key=str(secret_key or ""),
            )
        except Exception:
            short_code = None

    path = (
        build_public_invoice_short_relative_path(
            short_code=short_code,
            invoice_number=invoice_number,
        )
        if short_code
        else legacy_path
    )
    if base_url:
        base = str(base_url).rstrip("/")
        view_url = f"{base}{path}"
        legacy_view_url = f"{base}{legacy_path}"
    else:
        view_url = path
        legacy_view_url = legacy_path
    pdf_url = f"{view_url}/pdf"
    legacy_pdf_url = f"{legacy_view_url}/pdf"
    isdoc_url = f"{view_url}/isdoc"
    legacy_isdoc_url = f"{legacy_view_url}/isdoc"
    return {
        "view": view_url,
        "pdf": pdf_url,
        "pdf_download": f"{pdf_url}?download=1",
        "isdoc": isdoc_url,
        "isdoc_download": f"{isdoc_url}?download=1",
        "legacy_view": legacy_view_url,
        "legacy_pdf": legacy_pdf_url,
        "legacy_pdf_download": f"{legacy_pdf_url}?download=1",
        "legacy_isdoc": legacy_isdoc_url,
        "legacy_isdoc_download": f"{legacy_isdoc_url}?download=1",
        "short_code": short_code or "",
    }

from __future__ import annotations
from fakturek.time_utils import utc_now

"""SMTP email helpers (phase-22).

This module intentionally uses only the Python standard library.

Design goals:
- Simple, synchronous sending (good enough for small deployments).
- Testable (message construction separated from SMTP I/O).
- Conservative defaults (STARTTLS on, TLS off).
"""

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from typing import Iterable

import re
import smtplib
import ssl


def looks_like_email(value: str | None) -> bool:
    s = (value or "").strip()
    if not s or "@" not in s:
        return False
    # Very small sanity check. Real validation would be more involved.
    local, _, domain = s.partition("@")
    return bool(local) and bool(domain) and ("." in domain)


def split_recipients(value: str | None) -> list[str]:
    """Split comma/semicolon-separated recipients into normalized emails."""

    raw = (value or "").strip()
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    use_tls: bool = False
    use_starttls: bool = True
    timeout_seconds: float = 10.0
    from_email: str = ""
    from_name: str = ""


def is_configured(cfg: SMTPConfig) -> bool:
    return bool((cfg.host or "").strip())


_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


def _plain_text_to_html(body: str) -> str:
    text = str(body or "")
    parts: list[str] = []
    last = 0
    for match in _URL_RE.finditer(text):
        start, end = match.span()
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:)":
            trailing = url[-1] + trailing
            url = url[:-1]
            end -= 1
        parts.append(escape(text[last:start]))
        safe_url = escape(url, quote=True)
        parts.append(f'<a href="{safe_url}">{escape(url)}</a>')
        parts.append(escape(trailing))
        last = match.end()
    parts.append(escape(text[last:]))
    linked = "".join(parts).replace("\n", "<br>")
    return (
        '<!doctype html><html><body>'
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.5">'
        f"{linked}"
        "</div></body></html>"
    )


def build_email_message(
    *,
    from_email: str,
    from_name: str | None,
    to_emails: Iterable[str],
    cc_emails: Iterable[str] | None = None,
    subject: str,
    body: str,
    body_html: str | None = None,
    attachment_pdf: tuple[str, bytes] | None = None,
) -> EmailMessage:
    """Construct an EmailMessage.

    attachment_pdf: (filename, pdf_bytes)
    """

    msg = EmailMessage()
    from_addr = (from_email or "").strip()
    if not looks_like_email(from_addr):
        raise ValueError("Invalid from email")

    to_list = [e.strip() for e in to_emails if looks_like_email(e)]
    if not to_list:
        raise ValueError("Missing recipient")
    cc_list = [e.strip() for e in list(cc_emails or []) if looks_like_email(e)]

    display_name = (from_name or "").strip()
    msg["From"] = formataddr((display_name, from_addr)) if display_name else from_addr
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = (subject or "").strip()
    msg["Date"] = utc_now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1])

    # Keep a plain-text part for compatibility and add an HTML alternative so
    # invoice/public links are reliably clickable in mail clients.
    msg.set_content(body or "")
    msg.add_alternative(body_html or _plain_text_to_html(body or ""), subtype="html")

    if attachment_pdf is not None:
        filename, pdf_bytes = attachment_pdf
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=(filename or "invoice.pdf"),
        )

    return msg


def send_via_smtp(cfg: SMTPConfig, msg: EmailMessage) -> tuple[str | None, str | None]:
    """Send an email message via SMTP.

    Returns: (message_id, smtp_debug)
    """

    if not is_configured(cfg):
        raise RuntimeError("SMTP is not configured")

    host = (cfg.host or "").strip()
    port = int(cfg.port or 0)
    if port <= 0:
        raise ValueError("Invalid SMTP port")

    timeout = float(cfg.timeout_seconds or 10.0)

    # Prefer implicit TLS when explicitly requested.
    smtp: smtplib.SMTP
    if bool(cfg.use_tls):
        ctx = ssl.create_default_context()
        smtp = smtplib.SMTP_SSL(host=host, port=port, timeout=timeout, context=ctx)
    else:
        smtp = smtplib.SMTP(host=host, port=port, timeout=timeout)

    debug: str | None = None
    try:
        smtp.ehlo()
        if (not bool(cfg.use_tls)) and bool(cfg.use_starttls):
            ctx = ssl.create_default_context()
            smtp.starttls(context=ctx)
            smtp.ehlo()

        if cfg.username and cfg.password:
            smtp.login(cfg.username, cfg.password)

        smtp.send_message(msg)
        debug = "sent"
    finally:
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

    message_id = str(msg.get("Message-ID") or "").strip() or None
    return message_id, debug

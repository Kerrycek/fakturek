from __future__ import annotations

import base64
import hashlib
from typing import Any

try:  # pragma: no cover - optional dependency in some environments
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]


ENCRYPTED_PREFIX = "enc:v1:"
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_XML_META_TOKENS = (b"<!doctype", b"<!entity", b"\x00")


def _fernet_for_secret(secret_key: str, *, purpose: str) -> Fernet:
    if Fernet is None:
        raise RuntimeError("cryptography is required for encrypted secret storage")
    if not str(secret_key or "").strip():
        raise RuntimeError("A non-empty encryption key is required")
    material = hashlib.sha256(f"{purpose}:{secret_key}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_secret(value: str | None, *, secret_key: str, purpose: str = "secret-store") -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith(ENCRYPTED_PREFIX):
        return raw
    fernet = _fernet_for_secret(secret_key, purpose=purpose)
    token = fernet.encrypt(raw.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_secret(value: str | None, *, secret_key: str, purpose: str = "secret-store") -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not raw.startswith(ENCRYPTED_PREFIX):
        return raw
    token = raw[len(ENCRYPTED_PREFIX) :]
    fernet = _fernet_for_secret(secret_key, purpose=purpose)
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None
    except Exception:
        return None


def csv_safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text



def ensure_safe_xml_bytes(xml_bytes: bytes, *, max_bytes: int = 50 * 1024 * 1024) -> bytes:
    data = bytes(xml_bytes or b"")
    if not data:
        raise ValueError("Prázdné XML")
    if len(data) > int(max_bytes):
        raise ValueError("XML je příliš velké")
    # The XML has already been size-limited above, so scan the whole payload.
    # Scanning only a prefix lets attackers hide DOCTYPE/ENTITY after a long
    # comment or XML prolog padding and then trigger parser-level entity issues.
    low = data.lower()
    if any(token in low for token in _XML_META_TOKENS):
        raise ValueError("XML obsahuje nepovolené deklarace")
    return data

from __future__ import annotations

"""Authentication helpers.

Phase-13 introduces login/logout with session cookies.

We intentionally avoid extra dependencies (bcrypt/passlib) in early phases.
Passwords are stored as PBKDF2-HMAC-SHA256 hashes.

Hash format:

    pbkdf2_sha256$<iterations>$<salt_b64>$<digest_b64>

The format is versioned by the leading algorithm name so we can migrate later.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass


PBKDF2_ALG = "pbkdf2_sha256"
PBKDF2_HASH_NAME = "sha256"

# Conservative default (still reasonably fast on a small VPS).
DEFAULT_ITERATIONS = 260_000

SALT_BYTES = 16
DKLEN = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    s = data.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True)
class PasswordHash:
    alg: str
    iterations: int
    salt: bytes
    digest: bytes


def parse_password_hash(value: str) -> PasswordHash | None:
    """Parse a stored password hash.

    Returns None for unknown/invalid formats.
    """

    try:
        parts = (value or "").split("$")
        if len(parts) != 4:
            return None
        alg, it_s, salt_s, digest_s = parts
        if alg != PBKDF2_ALG:
            return None
        iterations = int(it_s)
        if iterations <= 0:
            return None
        salt = _b64d(salt_s)
        digest = _b64d(digest_s)
        if not salt or not digest:
            return None
        return PasswordHash(alg=alg, iterations=iterations, salt=salt, digest=digest)
    except Exception:
        return None


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Hash a password for storage."""

    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string")
    if iterations <= 0:
        raise ValueError("iterations must be > 0")

    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=DKLEN,
    )
    return f"{PBKDF2_ALG}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""

    if not isinstance(password, str) or not password:
        return False

    parsed = parse_password_hash(stored_hash)
    if parsed is None:
        return False

    digest = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        parsed.salt,
        parsed.iterations,
        dklen=len(parsed.digest),
    )
    return hmac.compare_digest(digest, parsed.digest)


def needs_rehash(stored_hash: str, *, iterations: int = DEFAULT_ITERATIONS) -> bool:
    """Return True when a stored hash should be upgraded."""

    parsed = parse_password_hash(stored_hash)
    if parsed is None:
        return True
    if parsed.alg != PBKDF2_ALG:
        return True
    return parsed.iterations < iterations

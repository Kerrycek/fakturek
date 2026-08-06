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
import functools
import hashlib
import hmac
import secrets
from dataclasses import dataclass


PBKDF2_ALG = "pbkdf2_sha256"
PBKDF2_HASH_NAME = "sha256"

# OWASP's current PBKDF2-HMAC-SHA256 baseline. Existing hashes are upgraded
# transparently after a successful login.
DEFAULT_ITERATIONS = 600_000
MAX_PBKDF2_ITERATIONS = 5_000_000

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

SALT_BYTES = 16
DKLEN = 32


def new_password_length_error(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Heslo může mít nejvýše {MAX_PASSWORD_LENGTH} znaků."
    return None


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
        if iterations <= 0 or iterations > MAX_PBKDF2_ITERATIONS:
            return None
        salt = _b64d(salt_s)
        digest = _b64d(digest_s)
        if not (8 <= len(salt) <= 64) or not (16 <= len(digest) <= 128):
            return None
        return PasswordHash(alg=alg, iterations=iterations, salt=salt, digest=digest)
    except Exception:
        return None


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Hash a password for storage."""

    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at most {MAX_PASSWORD_LENGTH} characters")
    if iterations <= 0 or iterations > MAX_PBKDF2_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_PBKDF2_ITERATIONS}")

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

    oversized_password = len(password) > MAX_PASSWORD_LENGTH
    password_bytes = (
        b"oversized-password-placeholder"
        if oversized_password
        else password.encode("utf-8")
    )
    parsed = parse_password_hash(stored_hash)
    invalid_hash = parsed is None
    if parsed is None:
        # Keep malformed hashes and unknown users on the expensive PBKDF2 path.
        # Otherwise the response time of the login endpoint reveals whether an
        # account exists before the application returns its generic error.
        parsed = _dummy_password_hash()

    digest = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        password_bytes,
        parsed.salt,
        parsed.iterations,
        dklen=len(parsed.digest),
    )
    return (
        not oversized_password
        and not invalid_hash
        and hmac.compare_digest(digest, parsed.digest)
    )


@functools.lru_cache(maxsize=1)
def _dummy_password_hash() -> PasswordHash:
    salt = hashlib.sha256(b"fakturek-password-verification-dummy-v1").digest()[:SALT_BYTES]
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        b"invalid-password-placeholder",
        salt,
        DEFAULT_ITERATIONS,
        dklen=DKLEN,
    )
    return PasswordHash(
        alg=PBKDF2_ALG,
        iterations=DEFAULT_ITERATIONS,
        salt=salt,
        digest=digest,
    )


def needs_rehash(stored_hash: str, *, iterations: int = DEFAULT_ITERATIONS) -> bool:
    """Return True when a stored hash should be upgraded."""

    parsed = parse_password_hash(stored_hash)
    if parsed is None:
        return True
    if parsed.alg != PBKDF2_ALG:
        return True
    return parsed.iterations < iterations

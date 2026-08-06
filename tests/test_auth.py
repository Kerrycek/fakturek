from pathlib import Path

import pytest

from fakturek.auth import (
    DEFAULT_ITERATIONS,
    MAX_PASSWORD_LENGTH,
    MAX_PBKDF2_ITERATIONS,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    new_password_length_error,
    parse_password_hash,
    verify_password,
)


def test_password_hash_roundtrip():
    # Keep iterations low in tests to stay fast.
    h = hash_password("correct horse battery staple", iterations=1_000)
    assert isinstance(h, str)
    assert h.startswith("pbkdf2_sha256$")

    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False


def test_password_hash_parse_and_needs_rehash():
    h = hash_password("pw", iterations=10)
    parsed = parse_password_hash(h)
    assert parsed is not None
    assert parsed.iterations == 10
    assert needs_rehash(h, iterations=11) is True
    assert needs_rehash(h, iterations=10) is False


def test_verify_password_rejects_unknown_format():
    assert verify_password("pw", "plain:pw") is False
    assert verify_password("pw", "") is False


def test_new_password_policy_and_default_work_factor():
    assert DEFAULT_ITERATIONS == 600_000
    assert new_password_length_error("short") == "Heslo musí mít alespoň 12 znaků."
    assert new_password_length_error("long-enough-12") is None
    assert new_password_length_error("x" * 1025) == "Heslo může mít nejvýše 1024 znaků."


def test_password_hash_limits_reject_resource_exhaustion_inputs():
    stored_hash = hash_password("correct horse battery staple", iterations=1_000)
    parts = stored_hash.split("$")
    parts[1] = str(MAX_PBKDF2_ITERATIONS + 1)

    assert parse_password_hash("$".join(parts)) is None
    assert verify_password("x" * (MAX_PASSWORD_LENGTH + 1), stored_hash) is False

    with pytest.raises(ValueError, match="at most"):
        hash_password("x" * (MAX_PASSWORD_LENGTH + 1), iterations=1_000)
    with pytest.raises(ValueError, match="iterations"):
        hash_password("valid-password", iterations=MAX_PBKDF2_ITERATIONS + 1)


def test_signup_client_password_policy_matches_server_policy():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "auth" / "signup.html"
    ).read_text(encoding="utf-8")

    assert f'minlength="{MIN_PASSWORD_LENGTH}"' in template
    assert f"value.length < {MIN_PASSWORD_LENGTH}" in template

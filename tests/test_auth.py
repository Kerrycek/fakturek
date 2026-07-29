from fakturek.auth import hash_password, needs_rehash, parse_password_hash, verify_password


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

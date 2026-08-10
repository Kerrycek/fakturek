from concurrent.futures import ThreadPoolExecutor

import pytest

from fakturek.bank_sync import safe_bank_sync_error_message
from fakturek.rate_limit import SlidingWindowRateLimiter
from fakturek.security import validate_outbound_base_url


def test_outbound_base_url_rejects_unsafe_schemes_and_credentials():
    assert validate_outbound_base_url("https://example.test/api/") == "https://example.test/api"

    for value in (
        "file:///etc/passwd",
        "ftp://example.test/data",
        "https://user:password@example.test/api",
        "https://example.test/api?token=secret",
        "https://example.test/api#fragment",
        "https://example.test:invalid/api",
        "https://example.test/path with space",
        "https://example.test/path\\segment",
    ):
        with pytest.raises(ValueError):
            validate_outbound_base_url(value)


def test_outbound_base_url_can_require_https():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_outbound_base_url("http://example.test/api", require_https=True)


def test_rate_limiter_is_thread_safe_for_one_bucket():
    limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)

    with ThreadPoolExecutor(max_workers=20) as executor:
        decisions = list(executor.map(lambda _: limiter.check("same-client"), range(40)))

    assert sum(decision.allowed for decision in decisions) == 10
    assert sum(not decision.allowed for decision in decisions) == 30


def test_rate_limiter_bucket_map_is_bounded():
    limiter = SlidingWindowRateLimiter(
        max_requests=5,
        window_seconds=60,
        max_buckets=25,
    )

    for index in range(500):
        limiter.check(f"client-{index}")

    assert limiter.bucket_count == 25


def test_rate_limiter_does_not_share_a_global_overflow_quota():
    limiter = SlidingWindowRateLimiter(
        max_requests=1,
        window_seconds=60,
        max_buckets=2,
    )

    assert limiter.check("client-a").allowed
    assert limiter.check("client-b").allowed
    assert limiter.check("client-c").allowed
    assert not limiter.check("client-c").allowed
    assert limiter.check("client-d").allowed
    assert limiter.bucket_count == 2


def test_bank_sync_errors_expose_only_operator_authored_messages():
    assert safe_bank_sync_error_message("Chybí Fio API token.") == "Chybí Fio API token."
    assert safe_bank_sync_error_message("") == ""

    hidden = safe_bank_sync_error_message("internal upstream diagnostic must not appear")
    assert hidden == "Synchronizace plateb se nepodařila. Podrobnosti jsou v serverovém logu."
    assert "internal upstream diagnostic" not in hidden

from __future__ import annotations

from fakturek.public_links import build_public_invoice_urls, slugify_public_invoice_number


def test_slugify_public_invoice_number_produces_readable_path_segment():
    assert slugify_public_invoice_number("INV 2026/0001") == "inv-2026-0001"
    assert slugify_public_invoice_number(" 2026-0002 ") == "2026-0002"
    assert slugify_public_invoice_number("") == "invoice"


def test_build_public_invoice_urls_include_invoice_number_in_short_path():
    urls = build_public_invoice_urls(
        public_username="demo-seller",
        token="tok-phase42",
        invoice_number="2026-0002",
        invoice_id=1,
        secret_key="test-secret",
    )

    assert urls["short_code"]
    assert urls["view"].startswith("/i/")
    assert urls["view"].endswith("/2026-0002")
    assert urls["pdf"].endswith("/2026-0002/pdf")
    assert urls["pdf_download"].endswith("/2026-0002/pdf?download=1")
    assert urls["isdoc"].endswith("/2026-0002/isdoc")
    assert urls["isdoc_download"].endswith("/2026-0002/isdoc?download=1")
    assert urls["legacy_view"] == "/demo-seller/i/tok-phase42/2026-0002"

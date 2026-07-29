from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings
from fakturek.public_links import build_public_invoice_urls


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path, *, public_base_url: str = ""):
    db_path = tmp_path / "phase42.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    if public_base_url:
        monkeypatch.setenv("PUBLIC_BASE_URL", public_base_url)
    else:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Demo Seller s.r.o.",
                email="owner@example.test",
                public_username="demo-seller",
                city="Praha",
                country="CZ",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Acme Client a.s.",
                email="billing@example.test",
                city="Praha",
                country="CZ",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2026-0002",
                status="issued",
                issue_date=date(2026, 2, 5),
                due_date=date(2026, 2, 10),
                currency="CZK",
                total_cents=2500000,
                buyer_name_cache="Acme Client a.s.",
                public_token="tok-phase42",
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def test_short_public_link_redirects_to_readable_canonical_path(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    public_urls = build_public_invoice_urls(
        public_username="demo-seller",
        token="tok-phase42",
        invoice_number="2026-0002",
        invoice_id=1,
        secret_key="test-secret",
    )
    short_code = public_urls["short_code"]

    redirect = client.get(f"/i/{short_code}", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == public_urls["view"]

    preview = client.get(public_urls["view"])
    assert preview.status_code == 200
    assert "Otevřít PDF" in preview.text

    _reset_settings_and_db()


def test_wrong_invoice_number_suffix_redirects_to_canonical_short_path(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    public_urls = build_public_invoice_urls(
        public_username="demo-seller",
        token="tok-phase42",
        invoice_number="2026-0002",
        invoice_id=1,
        secret_key="test-secret",
    )
    short_code = public_urls["short_code"]

    redirect_view = client.get(f"/i/{short_code}/spatne-cislo", follow_redirects=False)
    assert redirect_view.status_code == 307
    assert redirect_view.headers["location"] == public_urls["view"]

    redirect_pdf = client.get(f"/i/{short_code}/spatne-cislo/pdf", follow_redirects=False)
    assert redirect_pdf.status_code == 307
    assert redirect_pdf.headers["location"] == public_urls["pdf"]

    _reset_settings_and_db()


def test_invoice_detail_renders_readable_short_public_url(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(
        monkeypatch,
        tmp_path,
        public_base_url="https://invoice.example",
    )

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "https://invoice.example/i/" in detail.text
    assert "/2026-0002" in detail.text
    assert "/demo-seller/i/tok-phase42/2026-0002" not in detail.text

    _reset_settings_and_db()

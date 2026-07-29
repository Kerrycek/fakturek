from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "phase38.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
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
                public_token="tok-phase38",
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client


def test_invoice_detail_and_previews_share_unified_action_bar(monkeypatch, tmp_path):
    client = _setup_sqlite_app(monkeypatch, tmp_path)
    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "invoice-action-bar" in detail.text
    assert "Detail faktury" in detail.text
    assert "Přehled dokladu" in detail.text
    assert "Platba" in detail.text
    assert 'href="http://testserver/i/' in detail.text
    assert '/invoices/1/print"' not in detail.text
    assert "Sdílený odkaz" in detail.text
    assert 'id="send-email-dialog"' in detail.text
    assert 'class="section-disclosure card scroll-card" id="send-email"' not in detail.text

    edit = client.get("/invoices/1/edit")
    assert edit.status_code == 200
    assert 'href="http://testserver/i/' in edit.text
    assert 'class="invoice-editor-more-options">' in edit.text
    assert 'class="invoice-editor-more-options" open' not in edit.text

    preview = client.get("/invoices/1/print")
    assert preview.status_code == 200
    assert 'class="toolbar screen-only"' in preview.text
    assert "Faktura 2026-0002" in preview.text
    assert "Vytisknout" in preview.text

    public_preview = client.get("/demo-seller/i/tok-phase38/2026-0002")
    assert public_preview.status_code == 200
    assert 'class="toolbar screen-only"' in public_preview.text
    assert "Faktura 2026-0002" in public_preview.text
    assert "Stáhnout PDF" in public_preview.text

    _reset_settings_and_db()

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

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
    db_path = tmp_path / "phase54.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
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
                name="Bulk subject",
                email="owner@example.test",
                country="CZ",
                default_currency="CZK",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Acme Client",
                email="billing@example.test",
                city="Praha",
                country="CZ",
            )
        )
        db.add_all(
            [
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0001",
                    status="issued",
                    issue_date=date(2026, 3, 1),
                    due_date=date(2026, 3, 15),
                    currency="CZK",
                    total_cents=10_000,
                    buyer_name_cache="Acme Client",
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0002",
                    status="sent",
                    issue_date=date(2026, 3, 2),
                    due_date=date(2026, 3, 16),
                    currency="CZK",
                    total_cents=12_000,
                    buyer_name_cache="Acme Client",
                    sent_at=datetime(2026, 3, 2, 10, 0, 0),
                ),
                Invoice(
                    id=3,
                    subject_id=1,
                    contact_id=1,
                    number="DRAFT-3",
                    status="draft",
                    issue_date=date(2026, 3, 3),
                    due_date=date(2026, 3, 17),
                    currency="CZK",
                    total_cents=13_000,
                    buyer_name_cache="Acme Client",
                ),
                Invoice(
                    id=4,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0004",
                    status="paid",
                    issue_date=date(2026, 3, 4),
                    due_date=date(2026, 3, 18),
                    currency="CZK",
                    total_cents=14_000,
                    buyer_name_cache="Acme Client",
                    sent_at=datetime(2026, 3, 4, 11, 0, 0),
                    paid_on=date(2026, 3, 5),
                ),
            ]
        )
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal


def test_invoice_list_shows_bulk_toolbar(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices")

    assert response.status_code == 200
    assert 'action="/invoices/bulk-status"' in response.text
    assert "Vybrat vše na stránce" in response.text
    assert "Provést hromadně" in response.text
    assert 'data-bulk-checkbox' in response.text
    assert "Vystavit dobropis" in response.text
    assert "Možnosti" in response.text
    assert "Stornovat" in response.text
    assert "Smazat doklad" in response.text

    _reset_settings_and_db()


def test_bulk_mark_paid_updates_only_allowed_invoices(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/bulk-status",
        data={
            "action": "paid",
            "next": "/invoices?page=2",
            "invoice_ids": ["1", "2", "3"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    params = parse_qs(parts.query)
    assert parts.path == "/invoices"
    assert params["page"] == ["2"]
    assert "Označit jako zaplacené u 2 dokladů." in params["notice"][0]
    assert "1 vybraných dokladů jsem přeskočil" in params["error"][0]

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice_1 = db.get(Invoice, 1)
        invoice_2 = db.get(Invoice, 2)
        invoice_3 = db.get(Invoice, 3)
        assert invoice_1 is not None and invoice_1.status == "paid" and invoice_1.paid_on is not None
        assert invoice_2 is not None and invoice_2.status == "paid" and invoice_2.paid_on is not None
        assert invoice_3 is not None and invoice_3.status == "draft"

    _reset_settings_and_db()


def test_bulk_revert_steps_back_per_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/bulk-status",
        data={
            "action": "revert",
            "next": "/invoices",
            "invoice_ids": ["2", "4"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices?notice=")

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice_2 = db.get(Invoice, 2)
        invoice_4 = db.get(Invoice, 4)
        assert invoice_2 is not None and invoice_2.status == "issued"
        assert invoice_4 is not None and invoice_4.status == "sent"
        assert invoice_4.paid_on is None

    _reset_settings_and_db()


def test_bulk_status_requires_selection(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/bulk-status",
        data={
            "action": "paid",
            "next": "/invoices",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices?error=Nejd%C5%99%C3%ADv+vyber+aspo%C5%88+jednu+fakturu."

    _reset_settings_and_db()

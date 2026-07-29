from __future__ import annotations

from datetime import date, datetime

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
    db_path = tmp_path / "phase47.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, InvoiceSeries, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Test subject",
                email="owner@example.test",
                country="CZ",
                default_currency="CZK",
                public_username="test-subject",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Jiří Chvojka",
                email="jiri@example.test",
                ico="12345678",
                street="Dlouhá 1",
                city="Praha",
                zip="11000",
                country="CZ",
            )
        )
        db.add(
            InvoiceSeries(
                id=1,
                subject_id=1,
                name="default",
                prefix="2026-",
                pad_length=4,
                last_counter=0,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _create_and_issue_invoice(client: TestClient) -> None:
    create_response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "rounding_adjustment": "0.00",
            "notes": "Roční příspěvek",
            "item_description": ["Plnění A"],
            "item_quantity": ["1"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/invoices/1"


def test_paid_from_issued_can_be_reverted_back_to_issued(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    paid_response = client.post(
        "/invoices/1/status",
        data={"status": "paid", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert paid_response.status_code == 303

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Vrátit na vystavenou" in detail.text

    revert_response = client.post(
        "/invoices/1/status",
        data={"status": "issued", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert revert_response.status_code == 303
    assert revert_response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.sent_at is None
        assert invoice.paid_on is None
        assert invoice.issued_at is not None

    detail_after = client.get("/invoices/1")
    assert detail_after.status_code == 200
    assert "Vrátit na koncept" in detail_after.text

    _reset_settings_and_db()


def test_paid_from_sent_can_be_reverted_back_to_sent(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    sent_response = client.post(
        "/invoices/1/status",
        data={"status": "sent", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert sent_response.status_code == 303

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        sent_at_before = invoice.sent_at
        assert sent_at_before is not None

    paid_response = client.post(
        "/invoices/1/status",
        data={"status": "paid", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert paid_response.status_code == 303

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Vrátit na odeslanou" in detail.text

    revert_response = client.post(
        "/invoices/1/status",
        data={"status": "sent", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert revert_response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "sent"
        assert invoice.paid_on is None
        assert invoice.sent_at == sent_at_before

    _reset_settings_and_db()


def test_issued_can_be_reverted_to_draft_and_clears_pdf_metadata(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        invoice.pdf_path = "subjects/1/invoices/1/2026-0001.pdf"
        invoice.pdf_hash = "deadbeef"
        invoice.pdf_generated_at = datetime(2026, 3, 1, 10, 0, 0)
        db.commit()

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Vrátit na koncept" in detail.text

    revert_response = client.post(
        "/invoices/1/status",
        data={"status": "draft", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert revert_response.status_code == 303
    assert revert_response.headers["location"] == "/invoices/1"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        assert invoice.issued_at is None
        assert invoice.sent_at is None
        assert invoice.paid_on is None
        assert invoice.pdf_path is None
        assert invoice.pdf_hash is None
        assert invoice.pdf_generated_at is None

    detail_after = client.get("/invoices/1")
    assert detail_after.status_code == 200
    assert "Vystavit fakturu" in detail_after.text

    _reset_settings_and_db()


def test_issued_can_be_cancelled_and_restored(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    cancel_response = client.post(
        "/invoices/1/status",
        data={"status": "cancelled", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert cancel_response.status_code == 303

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "stornovaná" in detail.text

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "cancelled"

    restore_response = client.post(
        "/invoices/1/status",
        data={"status": "issued", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert restore_response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"

    detail_after = client.get("/invoices/1")
    assert detail_after.status_code == 200
    assert "Stornovat" in detail_after.text
    assert "Vrátit na koncept" in detail_after.text

    _reset_settings_and_db()


def test_paid_can_be_cancelled_and_restored(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    paid_response = client.post(
        "/invoices/1/status",
        data={"status": "paid", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert paid_response.status_code == 303

    detail_before = client.get("/invoices/1")
    assert detail_before.status_code == 200
    assert "Stornovat" in detail_before.text

    cancel_response = client.post(
        "/invoices/1/status",
        data={"status": "cancelled", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert cancel_response.status_code == 303

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "cancelled"
        assert invoice.paid_on is None

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "stornovaná" in detail.text
    assert "Vrátit na vystavenou" in detail.text

    restore_response = client.post(
        "/invoices/1/status",
        data={"status": "issued", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert restore_response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.paid_on is None

    _reset_settings_and_db()


def test_cancelled_invoice_print_shows_visible_cancelled_banner(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    cancel_response = client.post(
        "/invoices/1/status",
        data={"status": "cancelled", "next": "/invoices/1"},
        follow_redirects=False,
    )
    assert cancel_response.status_code == 303

    print_view = client.get("/invoices/1/print")
    assert print_view.status_code == 200
    assert "Stornováno" in print_view.text
    assert "Tento doklad byl stornován a neslouží k úhradě." in print_view.text

    _reset_settings_and_db()


def test_invoice_print_shows_payment_section_for_bank_transfer(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _create_and_issue_invoice(client)

    print_view = client.get("/invoices/1/print")
    assert print_view.status_code == 200
    assert "Platba" in print_view.text

    _reset_settings_and_db()


def test_draft_can_be_deleted(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                series_id=1,
                number="DRAFT-1",
                status="draft",
                issue_date=date(2026, 3, 1),
                due_date=date(2026, 3, 15),
                currency="CZK",
                buyer_name_cache="Jiří Chvojka",
                total_cents=10_000,
            )
        )
        db.commit()

    response = client.post("/invoices/1/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is None

    _reset_settings_and_db()

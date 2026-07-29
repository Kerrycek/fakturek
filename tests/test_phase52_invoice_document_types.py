from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit
from decimal import Decimal

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
    db_path = tmp_path / "phase52.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, InvoiceItem, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Test subject",
                email="owner@example.test",
                city="Praha",
                country="CZ",
                default_currency="CZK",
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
                ico="12345678",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2026-0042",
                status="issued",
                issue_date=date(2026, 3, 10),
                due_date=date(2026, 3, 24),
                currency="CZK",
                total_cents=12_000,
                buyer_name_cache="Acme Client a.s.",
            )
        )
        db.add(
            InvoiceItem(
                id=1,
                invoice_id=1,
                description="Správa infrastruktury",
                quantity=Decimal("2.00"),
                unit_price_cents=6_000,
                vat_rate=Decimal("0.00"),
                line_net_cents=12_000,
                line_vat_cents=0,
                line_total_cents=12_000,
                sort_order=1,
            )
        )
        db.commit()

    client = TestClient(create_app(), base_url="https://app.example.test")
    return client, SessionLocal


def test_proforma_uses_own_numbering_series(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    new_page = client.get("/invoices/new?document_type=proforma")
    assert new_page.status_code == 200
    assert "Nová zálohová faktura" in new_page.text
    assert "2026-ZAL-0001" in new_page.text

    create_response = client.post(
        "/invoices/new",
        data={
            "document_type": "proforma",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "rounding_adjustment": "0.00",
            "notes": "Záloha na práce",
            "item_description": ["Záloha na správu"],
            "item_quantity": ["1"],
            "item_unit_price": ["5000.00"],
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 303
    invoice_id = int(urlsplit(create_response.headers["location"]).path.rsplit("/", 1)[-1])

    issue_response = client.post(f"/invoices/{invoice_id}/issue", follow_redirects=False)
    assert issue_response.status_code == 303

    from fakturek.models import Invoice, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.document_type == "proforma"
        assert invoice.number == "2026-ZAL-0001"

        series = db.scalar(
            sqlalchemy.select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == 1)
            .where(InvoiceSeries.name == "proforma")
        )
        assert series is not None
        assert series.last_counter == 1

    _reset_settings_and_db()


def test_credit_note_is_created_from_existing_invoice_with_negative_items(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    detail_page = client.get("/invoices/1")
    assert detail_page.status_code == 200
    assert "Vystavit dobropis" in detail_page.text

    create_response = client.post("/invoices/1/credit-note", follow_redirects=False)
    assert create_response.status_code == 303
    credit_note_id = int(create_response.headers["location"].split("/")[-2])

    from fakturek.models import Invoice, InvoiceItem, InvoiceSeries

    with SessionLocal() as db:
        credit_note = db.get(Invoice, credit_note_id)
        assert credit_note is not None
        assert credit_note.document_type == "credit_note"
        assert credit_note.status == "draft"
        assert credit_note.source_invoice_id == 1
        assert credit_note.notes == "Dobropis k faktuře 2026-0042"

        items = db.scalars(
            sqlalchemy.select(InvoiceItem)
            .where(InvoiceItem.invoice_id == credit_note_id)
            .order_by(InvoiceItem.sort_order.asc())
        ).all()
        assert len(items) == 1
        assert items[0].unit_price_cents == -6_000
        assert items[0].line_total_cents == -12_000

    edit_response = client.post(
        f"/invoices/{credit_note_id}/edit",
        data={
            "document_type": "credit_note",
            "source_invoice_id": "1",
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "discount_amount": "0.00",
            "rounding_adjustment": "0.00",
            "notes": "Dobropis k faktuře 2026-0042",
            "item_description": ["Správa infrastruktury"],
            "item_quantity": ["2"],
            "item_unit": ["hod"],
            "item_unit_price": ["-60.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )
    assert edit_response.status_code == 303
    assert edit_response.headers["location"] == f"/invoices/{credit_note_id}"

    with SessionLocal() as db:
        items = db.scalars(
            sqlalchemy.select(InvoiceItem)
            .where(InvoiceItem.invoice_id == credit_note_id)
            .order_by(InvoiceItem.sort_order.asc())
        ).all()
        assert len(items) == 1
        assert items[0].unit == "hod"
        assert items[0].unit_price_cents == -6_000
        assert items[0].line_total_cents == -12_000

    issue_response = client.post(f"/invoices/{credit_note_id}/issue", follow_redirects=False)
    assert issue_response.status_code == 303
    assert issue_response.headers["location"] == f"/invoices/{credit_note_id}"

    with SessionLocal() as db:
        credit_note = db.get(Invoice, credit_note_id)
        assert credit_note is not None
        assert credit_note.number == "2026-DOB-0001"

        series = db.scalar(
            sqlalchemy.select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == 1)
            .where(InvoiceSeries.name == "credit_note")
        )
        assert series is not None
        assert series.last_counter == 1

    credit_note_detail = client.get(f"/invoices/{credit_note_id}")
    assert credit_note_detail.status_code == 200
    assert "Detail dobropisu" in credit_note_detail.text
    assert "Navázaný doklad" in credit_note_detail.text
    assert "2026-0042" in credit_note_detail.text

    credit_note_print = client.get(f"/invoices/{credit_note_id}/print")
    assert credit_note_print.status_code == 200
    assert "Dobropis" in credit_note_print.text
    assert "Navázaný doklad: 2026-0042" in credit_note_print.text

    filtered = client.get("/invoices?document_type=credit_note")
    assert filtered.status_code == 200
    assert "2026-DOB-0001" in filtered.text
    assert "2026-0042" not in filtered.text

    _reset_settings_and_db()

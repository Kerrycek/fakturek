from __future__ import annotations

from datetime import date
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


def _setup_sqlite_app(monkeypatch, tmp_path, *, is_vat_payer: bool = True, default_currency: str = "CZK"):
    db_path = tmp_path / "phase40.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
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
                is_vat_payer=is_vat_payer,
                default_currency=default_currency,
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


def test_invoice_editor_shows_discount_input_and_breakdown(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    res = client.get("/invoices/new")

    assert res.status_code == 200
    assert 'name="discount_amount"' in res.text
    assert 'data-discount-input' in res.text
    assert 'Mezisoučet' in res.text
    assert 'Sleva' in res.text

    _reset_settings_and_db()


def test_create_invoice_discount_affects_auto_rounding(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "discount_amount": "0.50",
            "rounding_enabled": "1",
            "notes": "Roční příspěvek",
            "item_description": ["Plnění A", ""],
            "item_quantity": ["1", "1"],
            "item_unit_price": ["100.00", ""],
            "item_vat_rate": ["21", "21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.discount_cents == 50
        assert invoice.rounding_adjustment_cents == 50
        assert invoice.total_cents == 12_100

    _reset_settings_and_db()


def test_create_invoice_rejects_discount_higher_than_subtotal(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "discount_amount": "500.00",
            "item_description": ["Plnění A"],
            "item_quantity": ["1"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Sleva nesmí být vyšší než mezisoučet." in response.text

    from fakturek.models import Invoice

    with SessionLocal() as db:
        assert db.get(Invoice, 1) is None

    _reset_settings_and_db()


def test_invoice_detail_and_print_show_discount_breakdown(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="DRAFT-1",
            status="draft",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
            currency="CZK",
            notes="Se slevou",
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            discount_cents=1_000,
            rounding_adjustment_cents=0,
            total_cents=11_100,
            series_id=1,
        )
        db.add(invoice)
        db.flush()
        db.add(
            InvoiceItem(
                invoice_id=1,
                description="Položka",
                quantity=Decimal("1.00"),
                unit_price_cents=10_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=10_000,
                line_vat_cents=2_100,
                line_total_cents=12_100,
                sort_order=1,
            )
        )
        db.commit()

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Mezisoučet" in detail.text
    assert "Sleva" in detail.text
    assert "-10,00 CZK" in detail.text

    print_view = client.get("/invoices/1/print")
    assert print_view.status_code == 200
    assert "-10,00 CZK" in print_view.text
    assert "Celkem" in print_view.text

    _reset_settings_and_db()


def test_edit_invoice_updates_discount(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="DRAFT-1",
            status="draft",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
            currency="CZK",
            notes="Původní poznámka",
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            discount_cents=0,
            rounding_adjustment_cents=0,
            total_cents=12_100,
            series_id=1,
        )
        db.add(invoice)
        db.flush()
        db.add(
            InvoiceItem(
                invoice_id=1,
                description="Původní položka",
                quantity=Decimal("1.00"),
                unit_price_cents=10_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=10_000,
                line_vat_cents=2_100,
                line_total_cents=12_100,
                sort_order=1,
            )
        )
        db.commit()

    response = client.post(
        "/invoices/1/edit",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-02",
            "due_date": "2026-03-16",
            "due_term": "14",
            "currency": "CZK",
            "discount_amount": "10.00",
            "rounding_adjustment": "0.00",
            "notes": "Upravená faktura",
            "item_description": ["Původní položka"],
            "item_quantity": ["1"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.discount_cents == 1_000
        assert invoice.total_cents == 11_100

    _reset_settings_and_db()

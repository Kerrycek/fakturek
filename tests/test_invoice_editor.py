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


def _setup_sqlite_app(monkeypatch, tmp_path, *, is_vat_payer: bool = True, default_currency: str = "CZK"):
    db_path = tmp_path / "invoice-editor.sqlite3"
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


def test_invoice_new_page_has_editor_and_inline_items(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    res = client.get("/invoices/new")

    assert res.status_code == 200
    assert "Položky faktury" in res.text
    assert "Přidat položku" in res.text
    assert "Vystavit fakturu" in res.text
    assert "Uložit koncept" in res.text
    assert "DRAFT-ID" in res.text
    assert 'data-drag-handle' in res.text
    assert 'data-duplicate-item' in res.text
    assert '/invoices/item-suggestions' in res.text
    assert '↑ ↓ Enter' in res.text
    assert 'ArrowDown' in res.text
    assert 'name="payment_method"' in res.text
    assert 'name="invoice_style"' in res.text
    assert 'name="item_unit"' in res.text
    assert 'invoice-unit-options' in res.text
    assert 'name="footer_mode"' in res.text
    assert 'name="footer_text"' in res.text
    assert 'data-contact-search' in res.text
    assert 'Začni psát název firmy, IČO nebo e-mail' in res.text
    assert 'value="" selected disabled>Vyber kontakt<' in res.text
    assert '<option value="1" selected' not in res.text
    assert "Chybí fakturační údaje vystavovatele" in res.text
    assert "Chybí bankovní účet" in res.text
    assert 'href="/settings#issuer"' in res.text
    assert 'href="/settings#bank-accounts"' in res.text
    assert 'id="bank_account_id"' in res.text
    assert res.text.index('id="bank_account_id"') < res.text.index("Další možnosti")

    _reset_settings_and_db()


def test_invoice_can_be_saved_as_draft_then_issued(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/new",
        data={
            "submit_action": "draft",
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "invoice_style": "modern",
            "footer_mode": "trade_register",
            "item_description": ["Rozpracovaná práce"],
            "item_quantity": ["1"],
            "item_unit": ["ks"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/1?notice=")

    from fakturek.models import Invoice, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        series = db.get(InvoiceSeries, 1)
        assert series is not None
        assert series.last_counter == 0

    list_response = client.get("/invoices?status=draft")
    assert list_response.status_code == 200
    assert "Koncepty:" in list_response.text
    assert "DRAFT-1" in list_response.text
    assert "Rozpracovaný koncept" in list_response.text

    issue_response = client.post("/invoices/1/issue", follow_redirects=False)
    assert issue_response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.number == "2026-0001"
        series = db.get(InvoiceSeries, 1)
        assert series is not None
        assert series.last_counter == 1

    _reset_settings_and_db()


def test_invoice_autosave_reuses_draft_and_preserves_numbering(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/autosave",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "invoice_style": "modern",
            "footer_mode": "trade_register",
            "item_description": ["První rozepsaná verze"],
            "item_quantity": ["1"],
            "item_unit": ["ks"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["number"] == "DRAFT-1"
    assert payload["invoice_id"] == 1

    response = client.post(
        "/invoices/autosave",
        data={
            "autosave_invoice_id": "1",
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "invoice_style": "modern",
            "footer_mode": "trade_register",
            "item_description": ["Druhá rozepsaná verze"],
            "item_quantity": ["2"],
            "item_unit": ["hod"],
            "item_unit_price": ["150.00"],
            "item_vat_rate": ["21"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["created"] is False
    assert payload["invoice_id"] == 1

    from fakturek.models import Invoice, InvoiceItem, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        assert invoice.total_cents == 36_300
        items = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == 1).all()
        assert [item.description for item in items] == ["Druhá rozepsaná verze"]
        series = db.get(InvoiceSeries, 1)
        assert series is not None
        assert series.last_counter == 0

    issue_response = client.post("/invoices/1/issue", follow_redirects=False)
    assert issue_response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.number == "2026-0001"
        series = db.get(InvoiceSeries, 1)
        assert series is not None
        assert series.last_counter == 1

    _reset_settings_and_db()


def test_create_invoice_with_inline_items(monkeypatch, tmp_path):
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
            "rounding_adjustment": "-0.50",
            "payment_method": "cash",
            "invoice_style": "modern",
            "footer_mode": "custom",
            "footer_text": "Fyzická osoba zapsaná v živnostenském rejstříku.",
            "notes": "Roční příspěvek",
            "item_description": [
                "Plnění A",
                "Plnění B",
                "",
            ],
            "item_quantity": ["2", "1", "1"],
            "item_unit": ["hod", "ks", ""],
            "item_unit_price": ["100.00", "50.00", ""],
            "item_vat_rate": ["21", "12", "21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.number == "2026-0001"
        assert invoice.total_cents == 29_750
        assert invoice.rounding_adjustment_cents == -50
        assert invoice.payment_method == "cash"
        assert invoice.invoice_style == "modern"
        assert invoice.footer_mode == "custom"
        assert invoice.footer_text == "Fyzická osoba zapsaná v živnostenském rejstříku."

        items = (
            db.query(InvoiceItem)
            .filter(InvoiceItem.invoice_id == 1)
            .order_by(InvoiceItem.sort_order.asc())
            .all()
        )
        assert [item.description for item in items] == ["Plnění A", "Plnění B"]
        assert [item.unit for item in items] == ["hod", "ks"]
        assert [item.line_total_cents for item in items] == [24_200, 5_600]

    _reset_settings_and_db()


def test_edit_invoice_replaces_inline_items(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from decimal import Decimal

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
            "rounding_adjustment": "0.00",
            "payment_method": "card",
            "invoice_style": "modern",
            "footer_mode": "commercial_register",
            "footer_text": "ignored on save",
            "notes": "Upravená faktura",
            "item_description": ["Nová položka", ""],
            "item_quantity": ["3", "1"],
            "item_unit": ["hod", ""],
            "item_unit_price": ["200.00", ""],
            "item_vat_rate": ["21", "21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.issue_date == date(2026, 3, 2)
        assert invoice.due_date == date(2026, 3, 16)
        assert invoice.notes == "Upravená faktura"
        assert invoice.payment_method == "card"
        assert invoice.invoice_style == "modern"
        assert invoice.footer_mode == "commercial_register"
        assert invoice.footer_text == "Společnost zapsaná v obchodním rejstříku."
        assert invoice.total_cents == 72_600

        items = (
            db.query(InvoiceItem)
            .filter(InvoiceItem.invoice_id == 1)
            .order_by(InvoiceItem.sort_order.asc())
            .all()
        )
        assert len(items) == 1
        assert items[0].description == "Nová položka"
        assert items[0].unit == "hod"
        assert items[0].line_total_cents == 72_600

    _reset_settings_and_db()


def test_invoice_new_page_for_non_vat_payer_hides_vat_and_uses_currency_select(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path, is_vat_payer=False, default_currency="EUR")

    res = client.get("/invoices/new")

    assert res.status_code == 200
    assert 'name="currency"' in res.text
    assert '<option value="EUR" selected' in res.text
    assert 'Neplátce DPH' in res.text
    assert 'invoice-items-th-vat' not in res.text

    _reset_settings_and_db()


def test_invoice_detail_for_draft_shows_workflow_actions(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="DRAFT-1",
            status="draft",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
            currency="CZK",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=0,
            series_id=1,
        )
        db.add(invoice)
        db.commit()

    response = client.get("/invoices/1")

    assert response.status_code == 200
    assert "DRAFT-1" in response.text
    assert "Obsah dokladu" in response.text
    assert "Vystavit fakturu" in response.text
    assert "Smazat doklad" in response.text

    _reset_settings_and_db()


def test_paid_cash_invoice_has_cash_receipt_page(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="2026-0001",
            status="paid",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 1),
            currency="CZK",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=12500,
            series_id=1,
            payment_method="cash",
            paid_on=date(2026, 3, 1),
        )
        db.add(invoice)
        db.commit()

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "/invoices/1/cash-receipt" in detail.text

    receipt = client.get("/invoices/1/cash-receipt")
    assert receipt.status_code == 200
    assert "Příjmový pokladní doklad" in receipt.text
    assert "PPD-2026-0001" in receipt.text
    assert "Originál pro plátce" in receipt.text
    assert "Kopie pro příjemce" in receipt.text

    _reset_settings_and_db()


def test_cash_receipt_requires_paid_cash_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="2026-0001",
            status="issued",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
            currency="CZK",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=12500,
            series_id=1,
            payment_method="cash",
        )
        db.add(invoice)
        db.commit()

    receipt = client.get("/invoices/1/cash-receipt")
    assert receipt.status_code == 400
    assert "Potvrzení o přijetí hotovosti můžeš vytisknout až po označení dokladu jako zaplaceného." in receipt.text

    _reset_settings_and_db()


def test_invoice_item_suggestions_endpoint_uses_past_invoice_items(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from decimal import Decimal

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        invoice_1 = Invoice(
            id=1,
            subject_id=1,
            number="2026-0001",
            status="issued",
            issue_date=date(2026, 2, 1),
            due_date=date(2026, 2, 15),
            currency="CZK",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=720_000,
            series_id=1,
        )
        invoice_2 = Invoice(
            id=2,
            subject_id=1,
            number="2026-0002",
            status="issued",
            issue_date=date(2026, 2, 20),
            due_date=date(2026, 3, 6),
            currency="CZK",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=720_000,
            series_id=1,
        )
        invoice_3 = Invoice(
            id=3,
            subject_id=1,
            number="2026-0003",
            status="issued",
            issue_date=date(2026, 2, 25),
            due_date=date(2026, 3, 11),
            currency="EUR",
            notes=None,
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=5000,
            series_id=1,
        )
        db.add_all([invoice_1, invoice_2, invoice_3])
        db.flush()
        db.add_all(
            [
                InvoiceItem(
                    invoice_id=1,
                    description="Platba členského příspěvku",
                    quantity=Decimal("1.00"),
                    unit_price_cents=720_000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=595_041,
                    line_vat_cents=124_959,
                    line_total_cents=720_000,
                    sort_order=1,
                ),
                InvoiceItem(
                    invoice_id=2,
                    description="Platba členského příspěvku",
                    quantity=Decimal("1.00"),
                    unit_price_cents=720_000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=595_041,
                    line_vat_cents=124_959,
                    line_total_cents=720_000,
                    sort_order=1,
                ),
                InvoiceItem(
                    invoice_id=3,
                    description="Platba členského příspěvku",
                    quantity=Decimal("1.00"),
                    unit_price_cents=5000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=4132,
                    line_vat_cents=868,
                    line_total_cents=5000,
                    sort_order=1,
                ),
            ]
        )
        db.commit()

    response = client.get(
        "/invoices/item-suggestions",
        params={
            "q": "člensk",
            "currency": "CZK",
            "limit": 8,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "suggestions" in payload
    assert payload["suggestions"]
    first = payload["suggestions"][0]
    assert first["description"] == "Platba členského příspěvku"
    assert first["quantity"] == "1"
    assert first["unit"] == ""
    assert first["unit_price"] == "7200.00"
    assert first["vat_rate"] == "21"
    assert first["invoice_number"] == "2026-0002"
    assert first["usage_count"] == 2

    _reset_settings_and_db()

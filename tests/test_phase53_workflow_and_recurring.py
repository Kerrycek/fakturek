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
    db_path = tmp_path / "phase53.sqlite3"
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
                status="paid",
                issue_date=date(2026, 3, 10),
                due_date=date(2026, 3, 24),
                currency="CZK",
                total_cents=12_000,
                payment_method="cash",
                paid_on=date(2026, 3, 10),
                buyer_name_cache="Acme Client a.s.",
            )
        )
        db.add(
            InvoiceItem(
                id=1,
                invoice_id=1,
                description="Správa za {{period_label}}",
                quantity=Decimal("2.00"),
                unit="hod",
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


def test_quote_uses_its_own_series_and_can_convert_to_proforma(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    new_page = client.get("/invoices/new?document_type=quote")
    assert new_page.status_code == 200
    assert "Nová nabídka" in new_page.text
    assert "2026-NAB-0001" in new_page.text

    create_response = client.post(
        "/invoices/new",
        data={
            "document_type": "quote",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "rounding_adjustment": "0.00",
            "notes": "Nabídka na správu",
            "item_description": ["Správa"],
            "item_quantity": ["1"],
            "item_unit": ["hod"],
            "item_unit_price": ["5000.00"],
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    quote_id = int(urlsplit(create_response.headers["location"]).path.split("/")[-1])

    issue_response = client.post(f"/invoices/{quote_id}/issue", follow_redirects=False)
    assert issue_response.status_code == 303

    convert_response = client.post(
        f"/invoices/{quote_id}/convert",
        data={"target_document_type": "proforma"},
        follow_redirects=False,
    )
    assert convert_response.status_code == 303
    proforma_id = int(convert_response.headers["location"].split("/")[-2])

    from fakturek.models import Invoice

    with SessionLocal() as db:
        quote = db.get(Invoice, quote_id)
        proforma = db.get(Invoice, proforma_id)
        assert quote is not None
        assert proforma is not None
        assert quote.document_type == "quote"
        assert quote.number == "2026-NAB-0001"
        assert proforma.document_type == "proforma"
        assert proforma.status == "draft"
        assert proforma.source_invoice_id == quote_id
        assert "navazuje na nabídku" in str(proforma.notes or "").lower()

    _reset_settings_and_db()


def test_credit_note_cannot_exceed_remaining_source_amount(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    create_response = client.post("/invoices/1/credit-note", follow_redirects=False)
    assert create_response.status_code == 303
    credit_note_id = int(create_response.headers["location"].split("/")[-2])

    issue_response = client.post(f"/invoices/{credit_note_id}/issue", follow_redirects=False)
    assert issue_response.status_code == 303

    second_response = client.post("/invoices/1/credit-note", follow_redirects=False)
    assert second_response.status_code == 303
    second_credit_id = int(second_response.headers["location"].split("/")[-2])

    invalid_edit = client.post(
        f"/invoices/{second_credit_id}/edit",
        data={
            "document_type": "credit_note",
            "source_invoice_id": "1",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "discount_amount": "0.00",
            "rounding_adjustment": "0.00",
            "notes": "Příliš velký dobropis",
            "item_description": ["Správa infrastruktury"],
            "item_quantity": ["3"],
            "item_unit": ["hod"],
            "item_unit_price": ["-60.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )
    assert invalid_edit.status_code == 400
    assert "Zbývá dobropisovat maximálně" in invalid_edit.text

    _reset_settings_and_db()


def test_recurring_plan_creates_new_invoice_with_resolved_tokens(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import InvoiceItem

    with SessionLocal() as db:
        item = db.get(InvoiceItem, 1)
        assert item is not None
        item.description = "Správa za {{period_label+1}} ({{month_start+1}} až {{month_end+1}}), měsíc {{month++}}"
        db.add(item)
        db.commit()

    plan_response = client.post(
        "/invoices/1/recurring",
        data={
            "name": "Měsíční správa",
            "interval_unit": "month",
            "interval_count": "1",
            "next_issue_date": date.today().isoformat(),
            "due_in_days": "10",
            "auto_issue": "1",
        },
        follow_redirects=False,
    )
    assert plan_response.status_code == 303

    run_response = client.post(
        "/recurring/1/run",
        data={"next": "/invoices"},
        follow_redirects=False,
    )
    assert run_response.status_code == 303

    from fakturek.models import Invoice, RecurringInvoicePlan

    with SessionLocal() as db:
        plan = db.get(RecurringInvoicePlan, 1)
        assert plan is not None
        created_invoice = db.get(Invoice, int(plan.last_generated_invoice_id or 0))
        assert created_invoice is not None
        assert created_invoice.status == "issued"
        assert created_invoice.source_invoice_id == 1

        created_item = db.scalar(
            sqlalchemy.select(InvoiceItem).where(InvoiceItem.invoice_id == int(created_invoice.id))
        )
        assert created_item is not None
        assert "{{" not in created_item.description
        assert "Správa za" in created_item.description
        assert " až " in created_item.description

    _reset_settings_and_db()


def test_recurring_page_can_create_new_recurring_template_without_existing_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    new_page = client.get("/invoices/new?recurring_mode=1")
    assert new_page.status_code == 200
    assert "Nová automatická faktura" in new_page.text
    assert "Nastavení automatické faktury" in new_page.text

    create_response = client.post(
        "/invoices/new",
        data={
            "recurring_mode": "1",
            "document_type": "invoice",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "rounding_adjustment": "0.00",
            "notes": "Správa za {{period_label}}",
            "name": "Měsíční správa hostingu",
            "next_issue_date": "2026-04-01",
            "interval_unit": "month",
            "interval_count": "1",
            "due_in_days": "10",
            "auto_issue": "1",
            "item_description": ["Správa za {{period_label}}"],
            "item_quantity": ["1"],
            "item_unit": ["měs"],
            "item_unit_price": ["1200.00"],
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert create_response.headers["location"].startswith("/recurring?notice=")

    from fakturek.models import Invoice, RecurringInvoicePlan

    with SessionLocal() as db:
        plan = db.get(RecurringInvoicePlan, 1)
        assert plan is not None
        assert plan.name == "Měsíční správa hostingu"
        assert plan.template_invoice_id is not None
        template_invoice = db.get(Invoice, int(plan.template_invoice_id))
        assert template_invoice is not None
        assert template_invoice.status == "draft"
        assert template_invoice.number.startswith("TPL-")
        assert "[[recurring-template]]" in str(template_invoice.internal_notes or "")

    recurring_response = client.get("/recurring")
    assert recurring_response.status_code == 200
    assert "Měsíční správa hostingu" in recurring_response.text
    assert "Interní šablona" in recurring_response.text
    assert "Upravit plán" not in recurring_response.text
    assert "Upravit šablonu" not in recurring_response.text
    assert 'href="/recurring/1/edit">Upravit</a>' in recurring_response.text

    invoice_list_response = client.get("/invoices")
    assert invoice_list_response.status_code == 200
    assert "TPL-" not in invoice_list_response.text
    assert "Měsíční správa hostingu" not in invoice_list_response.text

    _reset_settings_and_db()


def test_recurring_plan_can_be_edited(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    create_response = client.post(
        "/invoices/new",
        data={
            "recurring_mode": "1",
            "document_type": "invoice",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "rounding_adjustment": "0.00",
            "notes": "Správa za {{period_label+1}}",
            "name": "Původní plán",
            "next_issue_date": "2026-04-01",
            "interval_unit": "month",
            "interval_count": "1",
            "due_in_days": "10",
            "auto_issue": "1",
            "item_description": ["Správa za {{period_label+1}}"],
            "item_quantity": ["1"],
            "item_unit": ["měs"],
            "item_unit_price": ["1200.00"],
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303

    edit_page = client.get("/recurring/1/edit")
    assert edit_page.status_code == 200
    assert "Upravit automatickou fakturu" in edit_page.text
    assert "Kdy a jak vystavovat" in edit_page.text
    assert "Šablona automatické faktury" in edit_page.text
    assert "Uložit šablonu" in edit_page.text
    assert "{{month_name+1}}" in edit_page.text
    assert "{{issue_date+14d}}" in edit_page.text

    invalid_response = client.post(
        "/recurring/1/edit",
        data={
            "name": "Chybný plán",
            "next_issue_date": "2026-05-05",
            "interval_unit": "month",
            "interval_count": "1",
            "due_in_days": "14",
            "auto_send": "1",
        },
        follow_redirects=False,
    )
    assert invalid_response.status_code == 400
    assert "Automatické odeslání e-mailem" in invalid_response.text

    update_response = client.post(
        "/recurring/1/edit",
        data={
            "name": "Upravený měsíční plán",
            "next_issue_date": "2026-05-05",
            "interval_unit": "month",
            "interval_count": "2",
            "due_in_days": "21",
            "email_override": "ucetni@example.test",
            "is_active": "1",
            "auto_issue": "1",
            "auto_send": "1",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303
    assert update_response.headers["location"] == "/recurring/1/edit?plan_saved=1"

    from fakturek.models import RecurringInvoicePlan

    with SessionLocal() as db:
        plan = db.get(RecurringInvoicePlan, 1)
        assert plan is not None
        assert plan.name == "Upravený měsíční plán"
        assert plan.next_issue_date == date(2026, 5, 5)
        assert plan.interval_unit == "month"
        assert plan.interval_count == 2
        assert plan.due_in_days == 21
        assert plan.auto_issue is True
        assert plan.auto_send is True
        assert plan.email_override == "ucetni@example.test"

    template_update_response = client.post(
        "/invoices/2/edit?next=%2Frecurring%2F1%2Fedit%3Ftemplate_saved%3D1",
        data={
            "document_type": "invoice",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "taxable_supply_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "discount_amount": "0.00",
            "rounding_adjustment": "0.00",
            "notes": "Nová poznámka za {{period_label+1}}",
            "item_description": ["Upravená šablona za {{period_label+1}}"],
            "item_quantity": ["1"],
            "item_unit": ["měs"],
            "item_unit_price": ["1500.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )
    assert template_update_response.status_code == 303
    assert template_update_response.headers["location"] == "/recurring/1/edit?template_saved=1"

    from fakturek.models import InvoiceItem

    with SessionLocal() as db:
        template_item = db.scalar(sqlalchemy.select(InvoiceItem).where(InvoiceItem.invoice_id == 2))
        assert template_item is not None
        assert template_item.description == "Upravená šablona za {{period_label+1}}"
        assert plan.is_active is True

    _reset_settings_and_db()


def test_deleting_internal_recurring_template_removes_hidden_template_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    create_response = client.post(
        "/invoices/new",
        data={
            "recurring_mode": "1",
            "document_type": "invoice",
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "rounding_adjustment": "0.00",
            "notes": "Správa za {{period_label}}",
            "name": "Týdenní servis",
            "next_issue_date": "2026-04-01",
            "interval_unit": "week",
            "interval_count": "1",
            "due_in_days": "7",
            "item_description": ["Servis"],
            "item_quantity": ["1"],
            "item_unit": ["hod"],
            "item_unit_price": ["500.00"],
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303

    from fakturek.models import Invoice, RecurringInvoicePlan

    with SessionLocal() as db:
        plan = db.get(RecurringInvoicePlan, 1)
        assert plan is not None
        template_invoice_id = int(plan.template_invoice_id)

    delete_response = client.post("/recurring/1/delete", data={"next": "/recurring"}, follow_redirects=False)
    assert delete_response.status_code == 303

    with SessionLocal() as db:
        assert db.get(RecurringInvoicePlan, 1) is None
        assert db.get(Invoice, template_invoice_id) is None

    _reset_settings_and_db()


def test_paid_cash_invoice_has_pdf_receipt(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices/1/cash-receipt/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")

    _reset_settings_and_db()


def test_invoice_list_shows_credit_note_action_and_recurring_section(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    client.post(
        "/invoices/1/recurring",
        data={
            "name": "Měsíční správa",
            "interval_unit": "month",
            "interval_count": "1",
            "next_issue_date": date.today().isoformat(),
            "due_in_days": "10",
            "auto_issue": "1",
        },
        follow_redirects=False,
    )

    response = client.get("/invoices")

    assert response.status_code == 200
    assert "Dobropis" in response.text
    assert 'href="/recurring"' in response.text
    assert "Automatické doklady" not in response.text

    recurring_response = client.get("/recurring")

    assert recurring_response.status_code == 200
    assert "Automatické faktury" in recurring_response.text
    assert "Aktivní a pozastavená opakování" in recurring_response.text
    assert "Spustit teď" in recurring_response.text

    _reset_settings_and_db()

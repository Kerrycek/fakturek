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
    db_path = tmp_path / "phase32.sqlite3"
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


def test_create_invoice_generates_public_link_immediately(monkeypatch, tmp_path):
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
            "rounding_adjustment": "0.00",
            "notes": "Roční příspěvek",
            "item_description": ["Plnění A"],
            "item_quantity": ["1"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice, Subject

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        subject = db.get(Subject, 1)
        assert invoice is not None
        assert subject is not None
        assert invoice.status == "issued"
        assert invoice.number == "2026-0001"
        assert invoice.public_token
        assert subject.public_username

    page = client.get("/invoices/1")
    assert page.status_code == 200
    assert "Sdílený odkaz" in page.text

    _reset_settings_and_db()

def test_issue_proforma_uses_issue_year_prefix(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    create_response = client.post(
        "/invoices/new",
        data={
            "document_type": "proforma",
            "contact_id": "1",
            "issue_date": "2027-01-05",
            "due_date": "2027-01-19",
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

    issue_response = client.post("/invoices/1/issue", follow_redirects=False)
    assert issue_response.status_code == 303
    assert issue_response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.number == "2027-ZAL-0001"
        assert invoice.public_token

    _reset_settings_and_db()


def test_edit_after_issue_writes_audit_log(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import AuditLog, Invoice, InvoiceItem, SubjectBankAccount

    with SessionLocal() as db:
        account = SubjectBankAccount(
            id=1,
            subject_id=1,
            label="Hlavní účet",
            account_number="123456789/0100",
            iban="CZ6508000000192000145399",
            bic="GIBACZPX",
            country="CZ",
            is_default=True,
            sort_order=1,
        )
        db.add(account)
        invoice = Invoice(
            id=1,
            subject_id=1,
            number="2026-0001",
            status="issued",
            issue_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
            currency="CZK",
            notes="Původní poznámka",
            contact_id=1,
            buyer_name_cache="Jiří Chvojka",
            rounding_adjustment_cents=0,
            total_cents=12_100,
            series_id=1,
            bank_account_id=1,
            bank_account_label="Hlavní účet",
            bank_account_number="123456789/0100",
            bank_account_iban="CZ6508000000192000145399",
            bank_account_bic="GIBACZPX",
            bank_account_country="CZ",
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
            "issue_date": "2026-03-02",
            "due_date": "2026-03-16",
            "due_term": "14",
            "currency": "CZK",
            "bank_account_id": "1",
            "rounding_adjustment": "0.00",
            "notes": "Upravená vystavená faktura",
            "item_description": ["Nová položka"],
            "item_quantity": ["2"],
            "item_unit_price": ["200.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.number == "2026-0001"
        assert invoice.notes == "Upravená vystavená faktura"
        assert invoice.total_cents == 48_400

        audit_rows = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "invoice", AuditLog.entity_id == 1)
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert audit_rows
        assert audit_rows[0].action == "invoice_updated"

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Historie změn" in detail.text

    _reset_settings_and_db()


def test_delete_bank_account_keeps_invoice_snapshot(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, SubjectBankAccount

    with SessionLocal() as db:
        account = SubjectBankAccount(
            id=1,
            subject_id=1,
            label="Hlavní účet",
            account_number="123456789/0100",
            iban="CZ6508000000192000145399",
            bic="GIBACZPX",
            country="CZ",
            is_default=True,
            sort_order=1,
        )
        db.add(account)
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                number="2026-0001",
                status="issued",
                issue_date=date(2026, 3, 1),
                due_date=date(2026, 3, 15),
                currency="CZK",
                contact_id=1,
                buyer_name_cache="Jiří Chvojka",
                rounding_adjustment_cents=0,
                total_cents=12_100,
                series_id=1,
                bank_account_id=1,
                bank_account_label="Hlavní účet",
                bank_account_number="123456789/0100",
                bank_account_iban="CZ6508000000192000145399",
                bank_account_bic="GIBACZPX",
                bank_account_country="CZ",
            )
        )
        db.commit()

    response = client.post("/settings/accounts/1/delete", follow_redirects=False)
    assert response.status_code == 303

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        account = db.get(SubjectBankAccount, 1)
        assert account is None
        assert invoice is not None
        assert invoice.bank_account_id is None
        assert invoice.bank_account_number == "123456789/0100"
        assert invoice.bank_account_iban == "CZ6508000000192000145399"

    _reset_settings_and_db()


def test_payment_qr_helpers_cover_cz_and_sk_paths():
    from fakturek.banking import BankAccountPayload, build_payment_qr_codes

    account = BankAccountPayload(
        label="Hlavní účet",
        number="123456789/0100",
        iban="CZ6508000000192000145399",
        bic="GIBACZPX",
        country="CZ",
    )

    cz_codes = build_payment_qr_codes(
        account=account,
        amount_cents=12_345,
        currency="CZK",
        beneficiary_name="Test subject",
        invoice_number="2026-0001",
        due_date=date(2026, 3, 15),
    )
    assert {code.kind for code in cz_codes} >= {"cz_spd", "sk_bysquare"}

    eur_codes = build_payment_qr_codes(
        account=account,
        amount_cents=12_345,
        currency="EUR",
        beneficiary_name="Test subject",
        invoice_number="2026-0001",
        due_date=date(2026, 3, 15),
    )
    assert {code.kind for code in eur_codes} >= {"epc", "sk_bysquare"}

def test_invoice_autosave_creates_and_updates_draft_without_allocating_number(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    create = client.post(
        "/invoices/autosave",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "taxable_supply_date": "2026-03-01",
            "due_term": "14",
            "currency": "CZK",
            "rounding_adjustment": "0.00",
            "notes": "Průběžně uložený koncept",
            "item_description": ["Autosave položka A"],
            "item_quantity": ["1"],
            "item_unit": ["ks"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
    )

    assert create.status_code == 200
    payload = create.json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["status"] == "draft"
    assert payload["invoice_id"] == 1
    assert payload["number"] == "DRAFT-1"

    from fakturek.models import AuditLog, Invoice, InvoiceItem, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        series = db.get(InvoiceSeries, 1)
        assert invoice is not None
        assert series is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        assert invoice.public_token
        assert invoice.notes == "Průběžně uložený koncept"
        assert invoice.total_cents == 12_100
        assert series.last_counter == 0

        item = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == 1).one()
        assert item.description == "Autosave položka A"
        assert item.line_total_cents == 12_100

        audit = db.query(AuditLog).filter(AuditLog.entity_type == "invoice", AuditLog.entity_id == 1).one()
        assert audit.action == "invoice_draft_autosaved"

    update = client.post(
        "/invoices/autosave",
        data={
            "autosave_invoice_id": "1",
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-02",
            "due_date": "2026-03-16",
            "taxable_supply_date": "2026-03-02",
            "due_term": "14",
            "currency": "CZK",
            "rounding_adjustment": "0.00",
            "notes": "Upravený autosave koncept",
            "item_description": ["Autosave položka B"],
            "item_quantity": ["2"],
            "item_unit": ["hod"],
            "item_unit_price": ["200.00"],
            "item_vat_rate": ["21"],
        },
    )

    assert update.status_code == 200
    update_payload = update.json()
    assert update_payload["ok"] is True
    assert update_payload["created"] is False
    assert update_payload["invoice_id"] == 1

    with SessionLocal() as db:
        assert db.query(Invoice).count() == 1
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        assert invoice.issue_date == date(2026, 3, 2)
        assert invoice.due_date == date(2026, 3, 16)
        assert invoice.notes == "Upravený autosave koncept"
        assert invoice.total_cents == 48_400

        items = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == 1).all()
        assert len(items) == 1
        assert items[0].description == "Autosave položka B"
        assert items[0].unit == "hod"
        assert items[0].line_total_cents == 48_400

        series = db.get(InvoiceSeries, 1)
        assert series is not None
        assert series.last_counter == 0

    _reset_settings_and_db()


def test_invoice_draft_submit_then_issue_allocates_number_and_public_link(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/new",
        data={
            "submit_action": "draft",
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "taxable_supply_date": "2026-03-01",
            "due_term": "14",
            "currency": "CZK",
            "rounding_adjustment": "0.00",
            "notes": "Ručně uložený koncept",
            "item_description": ["Draft položka"],
            "item_quantity": ["1"],
            "item_unit": ["ks"],
            "item_unit_price": ["100.00"],
            "item_vat_rate": ["21"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/1")

    from fakturek.models import AuditLog, Invoice, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        series = db.get(InvoiceSeries, 1)
        assert invoice is not None
        assert series is not None
        assert invoice.status == "draft"
        assert invoice.number == "DRAFT-1"
        assert invoice.public_token
        assert invoice.issued_at is None
        assert invoice.total_cents == 12_100
        assert series.last_counter == 0

        audit = db.query(AuditLog).filter(AuditLog.entity_type == "invoice", AuditLog.entity_id == 1).one()
        assert audit.action == "invoice_draft_created"

    issue_response = client.post("/invoices/1/issue", follow_redirects=False)

    assert issue_response.status_code == 303
    assert issue_response.headers["location"] == "/invoices/1"

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        series = db.get(InvoiceSeries, 1)
        assert invoice is not None
        assert series is not None
        assert invoice.status == "issued"
        assert invoice.number == "2026-0001"
        assert invoice.public_token
        assert invoice.issued_at is not None
        assert series.last_counter == 1

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "2026-0001" in detail.text
    assert "Sdílený odkaz" in detail.text

    _reset_settings_and_db()

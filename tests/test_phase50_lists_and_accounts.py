from __future__ import annotations

from datetime import date, timedelta

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
    db_path = tmp_path / "phase50.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject, SubjectBankAccount

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="vpsFree.cz, z.s.",
                email="owner@example.test",
                city="Praha",
                country="CZ",
                ico="26568055",
                default_currency="CZK",
                default_invoice_footer_mode="association_register",
            )
        )
        db.add(
            SubjectBankAccount(
                id=1,
                subject_id=1,
                label="Hlavní účet",
                account_number="2200041594/2010",
                iban="CZ0420100000002200041594",
                bic="FIOBCZPP",
                country="CZ",
                currency="CZK",
                is_default=True,
                sort_order=1,
            )
        )

        for idx in range(1, 56):
            db.add(
                Contact(
                    id=idx,
                    subject_id=1,
                    name=f"Contact {idx:03d}",
                    email=f"contact{idx:03d}@example.test",
                    city="Praha",
                    country="CZ",
                )
            )

        base_issue_date = date(2026, 1, 1)
        for idx in range(1, 56):
            issue_date = base_issue_date + timedelta(days=idx - 1)
            db.add(
                Invoice(
                    id=idx,
                    subject_id=1,
                    contact_id=1,
                    number=f"2026-{idx:04d}",
                    status="issued",
                    issue_date=issue_date,
                    due_date=issue_date + timedelta(days=14),
                    currency="CZK",
                    total_cents=10_000 + idx,
                    buyer_name_cache="Contact 001",
                )
            )

        db.add(
            Invoice(
                id=233,
                subject_id=1,
                contact_id=1,
                number="2026-0233",
                status="issued",
                issue_date=date(2026, 3, 10),
                due_date=date(2026, 3, 24),
                currency="CZK",
                total_cents=70_000,
                buyer_name_cache="Contact 001",
                footer_mode="trade_register",
                footer_text=None,
                bank_account_id=None,
                bank_account_number=None,
                bank_account_iban=None,
            )
        )
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal


def test_contacts_list_supports_search_and_pagination(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    first_page = client.get("/contacts")
    assert first_page.status_code == 200
    assert "Contact 001" in first_page.text
    assert "Contact 050" in first_page.text
    assert "Contact 055" not in first_page.text

    second_page = client.get("/contacts?page=2")
    assert second_page.status_code == 200
    assert "Contact 055" in second_page.text
    assert "Contact 050" not in second_page.text

    filtered = client.get("/contacts?q=055")
    assert filtered.status_code == 200
    assert "Contact 055" in filtered.text
    assert "Contact 054" not in filtered.text

    _reset_settings_and_db()


def test_invoices_list_paginates(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    first_page = client.get("/invoices")
    assert first_page.status_code == 200
    assert "2026-0233" in first_page.text
    assert "2026-0001" not in first_page.text

    second_page = client.get("/invoices?page=2")
    assert second_page.status_code == 200
    assert "2026-0001" in second_page.text
    assert "2026-0233" not in second_page.text

    _reset_settings_and_db()


def test_imported_invoice_preview_falls_back_to_subject_footer(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices/233/print")

    assert response.status_code == 200
    assert "Spolek zapsaný ve spolkovém rejstříku." in response.text
    assert "Fyzická osoba zapsaná v živnostenském rejstříku." not in response.text

    _reset_settings_and_db()


def test_invoice_edit_prefills_default_bank_account_and_account_currency_is_editable(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    edit_page = client.get("/invoices/233/edit")
    assert edit_page.status_code == 200
    assert '<option value="1" selected' in edit_page.text
    assert "Hlavní účet · CZK" in edit_page.text

    settings_page = client.get("/settings?edit_account=1")
    assert settings_page.status_code == 200
    assert 'name="currency"' in settings_page.text
    assert 'class="settings-account-card"' in settings_page.text
    assert 'action="/settings/accounts/1/edit"' in settings_page.text
    assert 'id="account-1"' in settings_page.text

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Hlavní účet EUR",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "EUR",
            "is_default": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&edit_account=1#account-1"

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        assert account.currency == "EUR"

    _reset_settings_and_db()


def test_new_invoice_number_preview_and_issue_follow_imported_sequence(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    new_page = client.get("/invoices/new")
    assert new_page.status_code == 200
    assert "2026-0234" in new_page.text

    create_response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "issue_date": "2026-03-15",
            "due_date": "2026-03-29",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "bank_account_id": "1",
            "rounding_adjustment": "0.00",
            "notes": "Navazující faktura po importu",
            "item_description": ["Správa infrastruktury"],
            "item_quantity": ["1"],
            "item_unit_price": ["1000.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 303
    invoice_location = create_response.headers["location"]
    assert invoice_location.startswith("/invoices/")
    invoice_id = int(invoice_location.rsplit("/", 1)[-1])

    from fakturek.models import Invoice, InvoiceSeries

    with SessionLocal() as db:
        invoice = db.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.number == "2026-0234"
        assert invoice.status == "issued"

        default_series = db.scalar(
            sqlalchemy.select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == 1)
            .where(InvoiceSeries.name == "default")
        )
        assert default_series is not None
        assert default_series.last_counter == 234
        assert default_series.last_counter_year == 2026

    _reset_settings_and_db()


def test_invoice_can_be_duplicated_into_new_issued_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from decimal import Decimal

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        source = db.get(Invoice, 233)
        assert source is not None
        source.notes = "Původní poznámka"
        source.payment_method = "bank_transfer"
        source.bank_account_id = 1
        source.bank_account_number = "2200041594/2010"
        source.bank_account_iban = "CZ0420100000002200041594"
        source.bank_account_bic = "FIOBCZPP"
        source.bank_account_country = "CZ"
        db.add(
            InvoiceItem(
                invoice_id=233,
                description="Správa infrastruktury",
                quantity=Decimal("2.00"),
                unit_price_cents=35_000,
                vat_rate=Decimal("0.00"),
                line_net_cents=70_000,
                line_vat_cents=0,
                line_total_cents=70_000,
                sort_order=1,
            )
        )
        db.commit()

    response = client.post("/invoices/233/duplicate", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/")
    assert "/edit?duplicated=1&from=2026-0233" in response.headers["location"]

    duplicated_invoice_id = int(response.headers["location"].split("/")[-2])

    with SessionLocal() as db:
        duplicated = db.get(Invoice, duplicated_invoice_id)
        assert duplicated is not None
        assert duplicated.status == "draft"
        assert duplicated.number.startswith("DRAFT-")
        assert duplicated.contact_id == 1
        assert duplicated.notes == "Původní poznámka"
        assert duplicated.payment_method == "bank_transfer"
        assert duplicated.bank_account_number == "2200041594/2010"
        assert duplicated.bank_account_iban == "CZ0420100000002200041594"
        assert (duplicated.due_date - duplicated.issue_date).days == 14

        duplicated_items = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == duplicated_invoice_id).all()
        assert len(duplicated_items) == 1
        assert duplicated_items[0].description == "Správa infrastruktury"
        assert duplicated_items[0].line_total_cents == 70_000

    detail_page = client.get("/invoices/233")
    assert detail_page.status_code == 200
    assert "Duplikovat doklad" in detail_page.text

    duplicated_edit = client.get(response.headers["location"])
    assert duplicated_edit.status_code == 200
    assert "Nová zduplikovaná faktura" in duplicated_edit.text
    assert "Vychází z dokladu 2026-0233." in duplicated_edit.text
    assert "Vystavit nový doklad" in duplicated_edit.text

    save_copy = client.post(
        response.headers["location"],
        data={
            "contact_id": "1",
            "issue_date": "2026-03-16",
            "due_date": "2026-03-30",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "bank_account_id": "1",
            "variable_symbol": "20260234",
            "rounding_adjustment": "0.00",
            "notes": "Zduplikovaná kopie",
            "item_description": ["Správa infrastruktury"],
            "item_quantity": ["2"],
            "item_unit_price": ["610.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )
    assert save_copy.status_code == 303

    duplicate_detail = client.get(save_copy.headers["location"])
    assert duplicate_detail.status_code == 200
    assert "Kopie dokladu vytvořena" in duplicate_detail.text

    with SessionLocal() as db:
        duplicated = db.get(Invoice, duplicated_invoice_id)
        assert duplicated is not None
        assert duplicated.status == "issued"
        assert duplicated.number == "2026-0234"

    _reset_settings_and_db()

import pytest
from fastapi.testclient import TestClient
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.main import create_app
from fakturek.models import Contact, Invoice, InvoiceSeries, Subject
from fakturek.settings import get_settings


@pytest.fixture(autouse=True)
def _disable_auth_for_page_smoke_tests(monkeypatch, tmp_path):
    db_path = tmp_path / "page-smoke.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None
    from fakturek.db import get_engine, get_sessionmaker

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(Subject(id=1, name="Smoke Subject", email="smoke@example.test", country="CZ", default_currency="CZK"))
        db.add(Contact(id=1, subject_id=1, name="Smoke Contact", email="contact@example.test", country="CZ"))
        db.add(InvoiceSeries(id=1, subject_id=1, name="default", prefix="2026-", pad_length=4, last_counter=1))
        db.add(Invoice(id=1, subject_id=1, contact_id=1, series_id=1, number="2026-0001", status="issued", issue_date=date(2026, 1, 1), due_date=date(2026, 1, 15), currency="CZK", total_cents=10000))
        db.commit()
    yield
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_contact_detail_app(monkeypatch, tmp_path):
    db_path = tmp_path / "contact-detail.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker

    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(Subject(id=1, name="Test subject", email="owner@example.test"))
        db.add(Contact(id=1, subject_id=1, name="Acme Client", email="acme@example.test"))
        db.add(InvoiceSeries(id=1, subject_id=1, name="default", prefix="2026-", pad_length=4, last_counter=3))
        db.add_all(
            [
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    series_id=1,
                    number="2026-0001",
                    status="paid",
                    issue_date=date(2026, 1, 10),
                    due_date=date(2026, 1, 24),
                    currency="CZK",
                    total_cents=10000,
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    contact_id=1,
                    series_id=1,
                    number="2026-0002",
                    status="issued",
                    issue_date=date(2026, 3, 10),
                    due_date=date(2026, 3, 24),
                    currency="CZK",
                    total_cents=20000,
                ),
                Invoice(
                    id=3,
                    subject_id=1,
                    contact_id=1,
                    series_id=1,
                    number="2026-0003",
                    status="sent",
                    issue_date=date(2026, 2, 10),
                    due_date=date(2026, 2, 24),
                    currency="CZK",
                    total_cents=30000,
                ),
            ]
        )
        db.commit()

    return TestClient(create_app(), base_url="https://app.example.test")


def test_stats_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/stats")
    assert res.status_code == 200
    assert "statistik" in res.text.lower()


def test_settings_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/settings")
    assert res.status_code == 200
    assert "nastaven" in res.text.lower()


def test_contacts_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/contacts")
    assert res.status_code == 200
    assert "kontakt" in res.text.lower()


def test_invoices_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()


def test_invoices_page_ok_with_filters():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices?status=paid&q=test&overdue=true")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()


def test_contacts_new_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/contacts/new")
    assert res.status_code == 200
    assert "kontakt" in res.text.lower()
    assert 'name="csrf_token"' in res.text


def test_invoices_new_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices/new")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()


def test_contacts_detail_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/contacts/1")
    assert res.status_code == 200
    assert "kontakt" in res.text.lower()


def test_contacts_detail_lists_newest_invoices_first(monkeypatch, tmp_path):
    client = _setup_sqlite_contact_detail_app(monkeypatch, tmp_path)

    res = client.get("/contacts/1")

    assert res.status_code == 200
    assert res.text.index("2026-0002") < res.text.index("2026-0003") < res.text.index("2026-0001")

    _reset_settings_and_db()


def test_base_navigation_lists_contacts_before_invoices():
    template = (PROJECT_ROOT / "templates/base.html").read_text(encoding="utf-8")
    contacts_pos = template.index('/contacts')
    invoices_pos = template.index('/invoices')
    assert contacts_pos < invoices_pos


def test_contacts_template_has_live_search_autosubmit():
    template = (PROJECT_ROOT / "templates/contacts/list.html").read_text(encoding="utf-8")
    assert "data-live-search-input" in template
    assert "data-live-search-form" in template


def test_contacts_edit_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/contacts/1/edit")
    assert res.status_code == 200
    assert "kontakt" in res.text.lower()


def test_contacts_edit_template_includes_csrf_input():
    template = (PROJECT_ROOT / "templates/contacts/edit.html").read_text(encoding="utf-8")
    assert "{{ csrf_input(request)|safe }}" in template


def test_invoices_detail_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices/1")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()


def test_invoices_edit_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices/1/edit")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()


def test_invoices_print_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/invoices/1/print")
    assert res.status_code == 200
    assert "faktur" in res.text.lower()

def test_imports_page_ok():
    client = TestClient(create_app(), base_url="https://app.example.test")
    res = client.get("/imports")
    assert res.status_code == 200
    assert "export" in res.text.lower()
    assert "import" in res.text.lower()

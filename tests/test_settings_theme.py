from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "settings-theme.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Seller One s.r.o.",
                    email="owner@example.test",
                    city="Praha",
                    country="CZ",
                    default_currency="CZK",
                    ico="12345678",
                ),
                Contact(
                    id=1,
                    subject_id=1,
                    name="Acme Client a.s.",
                    email="billing@example.test",
                    city="Praha",
                    country="CZ",
                ),
                User(
                    id=1,
                    username="demo",
                    email="demo@example.test",
                    password_hash=hash_password("secret123", iterations=1_000),
                    is_active=True,
                    ui_theme="light",
                ),
                UserSubject(
                    id=1,
                    user_id=1,
                    subject_id=1,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0001",
                    status="sent",
                    issue_date=date(2026, 1, 2),
                    due_date=date(2026, 1, 9),
                    currency="CZK",
                    total_cents=10_000,
                ),
            ]
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _login(client: TestClient, password: str = "secret123") -> None:
    response = client.post(
        "/login",
        data={
            "identifier": "demo",
            "password": password,
            "next": "/settings",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_settings_page_shows_theme_picker(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Motiv aplikace" in response.text
    assert "Tmavý motiv" in response.text
    assert 'name="ui_theme"' in response.text

    _reset_settings_and_db()


def test_settings_page_shows_relevant_tax_subject_controls(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Typ subjektu" in response.text
    assert "Spolek / nezisková organizace" in response.text
    assert 'data-tax-legal-form' in response.text
    assert 'data-flat-tax-field' in response.text
    assert "U spolku nebo nekomerčního subjektu" in response.text

    _reset_settings_and_db()


def test_settings_can_change_theme_and_persist(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.post(
        "/settings/theme",
        data={"ui_theme": "dark"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1#appearance"

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert 'data-fakturek-theme-mode="dark"' in settings_page.text
    assert 'theme-mode-dark' in settings_page.text

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert user.ui_theme == "dark"

    fresh_client = TestClient(client.app)
    _login(fresh_client)
    fresh_settings_page = fresh_client.get("/settings")
    assert fresh_settings_page.status_code == 200
    assert 'data-fakturek-theme-mode="dark"' in fresh_settings_page.text
    assert 'theme-mode-dark' in fresh_settings_page.text

    _reset_settings_and_db()


def test_settings_can_change_language_and_translate_ui(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.post(
        "/settings/language",
        data={"ui_language": "en", "next": "/settings"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert '<html lang="en"' in settings_page.text
    assert 'Account and billing settings' in settings_page.text
    assert 'Interface language' in settings_page.text
    assert 'Jazyk prostředí' not in settings_page.text
    assert 'name="ui_language"' in settings_page.text

    invoices_page = client.get("/invoices")
    assert invoices_page.status_code == 200
    assert 'Documents' in invoices_page.text
    assert 'New invoice' in invoices_page.text

    # UI language must not rewrite legal invoice print/PDF documents;
    # those keep using the per-invoice invoice_language setting.
    print_page = client.get("/invoices/1/print")
    assert print_page.status_code == 200
    assert '<html lang="cs"' in print_page.text
    assert 'Faktura 2026-0001' in print_page.text
    assert 'Invoice 2026-0001' not in print_page.text

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert user.ui_language == "en"

    fresh_client = TestClient(client.app)
    _login(fresh_client)
    fresh_settings_page = fresh_client.get("/settings")
    assert fresh_settings_page.status_code == 200
    assert '<html lang="en"' in fresh_settings_page.text
    assert 'Account and billing settings' in fresh_settings_page.text

    _reset_settings_and_db()

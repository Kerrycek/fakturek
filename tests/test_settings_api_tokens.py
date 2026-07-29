from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
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
    db_path = tmp_path / "settings-api-tokens.sqlite3"
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


def test_settings_page_shows_api_access_section(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "API přístup" in response.text
    assert "Otevřít API dokumentaci" in response.text
    assert 'action="/settings/api-tokens/create"' in response.text

    _reset_settings_and_db()


def test_dev_subdomain_uses_isolated_secure_session_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("APP_BASE_URL", "https://142.fakturek.cz")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://142.fakturek.cz")
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("https://142.fakturek.cz/login")

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "fakturek_142_fakturek_cz_session=" in set_cookie
    assert "Domain=.fakturek.cz" not in set_cookie
    assert "secure" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()

    _reset_settings_and_db()


def test_settings_can_create_and_revoke_api_token(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    create_response = client.post(
        "/settings/api-tokens/create",
        data={"name": "WordPress integrace", "expires_in_days": "30", "subject_id": "1"},
        follow_redirects=True,
    )

    assert create_response.status_code == 200
    assert "Nový API klíč: WordPress integrace" in create_response.text
    assert "ftk_pat_" in create_response.text
    assert "WordPress integrace" in create_response.text
    session_cookie = client.cookies.get("fakturek_session") or ""
    assert "ftk_pat_" not in session_cookie
    assert "api_token_created" not in session_cookie

    from fakturek.models import ApiToken

    with SessionLocal() as db:
        token = db.scalar(select(ApiToken).where(ApiToken.user_id == 1))
        assert token is not None
        assert token.name == "WordPress integrace"
        token_id = int(token.id)

    fresh_settings = client.get("/settings")
    assert fresh_settings.status_code == 200
    assert "Nový API klíč: WordPress integrace" not in fresh_settings.text
    assert "WordPress integrace" in fresh_settings.text
    assert "Aktivní" in fresh_settings.text

    revoke_response = client.post(
        f"/settings/api-tokens/{token_id}/revoke",
        follow_redirects=True,
    )

    assert revoke_response.status_code == 200
    assert "Odvolaný" in revoke_response.text

    with SessionLocal() as db:
        token = db.get(ApiToken, token_id)
        assert token is not None
        assert token.revoked_at is not None

    _reset_settings_and_db()

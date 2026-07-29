from __future__ import annotations

from datetime import date
import re

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.auth import hash_password, verify_password
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "settings-password.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "robot@example.test")
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
    client = TestClient(app, base_url="https://app.example.test")
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


def test_settings_page_shows_security_section(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Změna hesla" in response.text
    assert "Můj profil" in response.text

    _reset_settings_and_db()


def test_login_ignores_browser_asset_next_urls(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/login?next=/apple-touch-icon.png")
    assert response.status_code == 200
    assert 'name="next" value="/"' in response.text

    login = client.post(
        "/login",
        data={
            "identifier": "demo",
            "password": "secret123",
            "next": "/apple-touch-icon.png",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"

    _reset_settings_and_db()


def test_settings_can_change_current_user_password(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.post(
        "/settings/password",
        data={
            "current_password": "secret123",
            "new_password": "noveheslo123",
            "new_password2": "noveheslo123",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1#security"

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert verify_password("noveheslo123", str(user.password_hash))
        assert not verify_password("secret123", str(user.password_hash))

    fresh_client = TestClient(client.app, base_url="https://app.example.test")
    old_login = fresh_client.post(
        "/login",
        data={"identifier": "demo", "password": "secret123", "next": "/"},
        follow_redirects=False,
    )
    assert old_login.status_code == 401

    new_login = fresh_client.post(
        "/login",
        data={"identifier": "demo", "password": "noveheslo123", "next": "/"},
        follow_redirects=False,
    )
    assert new_login.status_code == 303

    _reset_settings_and_db()


def test_settings_rejects_wrong_current_password(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    response = client.post(
        "/settings/password",
        data={
            "current_password": "spatneheslo",
            "new_password": "noveheslo123",
            "new_password2": "noveheslo123",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Současné heslo nesedí." in response.text
    assert "spatneheslo" not in response.text
    assert "noveheslo123" not in response.text

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert verify_password("secret123", str(user.password_hash))

    _reset_settings_and_db()


def test_password_reset_flow_changes_password_and_invalidates_token(monkeypatch, tmp_path):
    sent_messages: list[object] = []

    def fake_send_via_smtp(_cfg, msg):
        sent_messages.append(msg)
        return "<reset@example.test>", "sent"

    monkeypatch.setattr("fakturek.main.send_via_smtp", fake_send_via_smtp)
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "/password/reset" in login_page.text

    reset_page = client.get("/password/reset")
    assert reset_page.status_code == 200
    assert "Zapomenuté heslo" in reset_page.text

    requested = client.post(
        "/password/reset",
        data={"mode": "request", "email": "demo@example.test"},
    )
    assert requested.status_code == 200
    assert "Pokud u nás tenhle e-mail existuje" in requested.text
    assert len(sent_messages) == 1

    body = ""
    for part in sent_messages[0].walk():
        if part.get_content_type() == "text/plain":
            body = part.get_content()
            break
    match = re.search(r"/password/reset\?token=([^\s]+)", body)
    assert match is not None
    token = match.group(1)

    form = client.get(f"/password/reset?token={token}")
    assert form.status_code == 200
    assert "Nastavení nového hesla" in form.text

    changed = client.post(
        "/password/reset",
        data={
            "mode": "reset",
            "token": token,
            "new_password": "noveheslo456",
            "new_password2": "noveheslo456",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"] == "/login?reset=1"

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert verify_password("noveheslo456", str(user.password_hash))
        assert not verify_password("secret123", str(user.password_hash))

    old_login = client.post(
        "/login",
        data={"identifier": "demo@example.test", "password": "secret123", "next": "/"},
        follow_redirects=False,
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/login",
        data={"identifier": "demo@example.test", "password": "noveheslo456", "next": "/"},
        follow_redirects=False,
    )
    assert new_login.status_code == 303

    reused = client.post(
        "/password/reset",
        data={
            "mode": "reset",
            "token": token,
            "new_password": "jineheslo789",
            "new_password2": "jineheslo789",
        },
    )
    assert reused.status_code == 400
    assert "už není platný" in reused.text

    _reset_settings_and_db()

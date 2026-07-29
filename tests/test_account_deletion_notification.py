from __future__ import annotations

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


def test_account_deletion_sends_owner_notification(monkeypatch, tmp_path):
    db_path = tmp_path / "account-deletion-notification.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Fakturek.cz")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(id=1, name="Delete Test s.r.o.", email="subject@example.test", country="CZ"),
                User(
                    id=1,
                    username="delete-me",
                    email="delete-me@example.test",
                    password_hash=hash_password("secret123", iterations=1_000),
                    is_active=True,
                ),
                UserSubject(id=1, user_id=1, subject_id=1, role="owner", can_view=True, can_edit=True, can_issue=True),
            ]
        )
        db.commit()

    sent_messages = []

    def _fake_send(_cfg, msg):
        sent_messages.append(msg)
        return "<delete@example.test>", "sent"

    monkeypatch.setattr("fakturek.main.send_via_smtp", _fake_send)

    client = TestClient(create_app())
    login = client.post(
        "/login",
        data={"identifier": "delete-me", "password": "secret123", "next": "/settings"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Zrušení účtu" in page.text
    assert 'name="reason"' in page.text
    assert 'name="reason" required' not in page.text

    response = client.post(
        "/settings/account/delete",
        data={
            "password": "secret123",
            "confirmation": "SMAZAT ÚČET",
            "reason": "Už nepotřebuju fakturovat.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?info=account-deletion-requested"

    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert str(message.get("To")) == "noreply@example.test"
    body = next(part.get_content() for part in message.walk() if part.get_content_type() == "text/plain")
    assert "delete-me" in body
    assert "delete-me@example.test" in body
    assert "Už nepotřebuju fakturovat." in body
    assert "Subjekty s přístupem: 1" in body

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert user.is_active is False
        assert user.deletion_reason == "Už nepotřebuju fakturovat."

    _reset_settings_and_db()

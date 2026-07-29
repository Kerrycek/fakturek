from __future__ import annotations

import re

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


def test_login_submit_regression_select_shadowing(monkeypatch, tmp_path):
    db_path = tmp_path / "login-flow.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(Subject(id=1, name="Test subject", email="owner@example.test"))
        db.add(
            User(
                id=1,
                username="demo",
                email="demo@example.test",
                password_hash=hash_password("secret123", iterations=1_000),
                is_active=True,
            )
        )
        db.add(
            UserSubject(
                id=1,
                user_id=1,
                subject_id=1,
                can_view=True,
                can_edit=True,
                can_issue=True,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)

    login_page = client.get("/login")
    assert login_page.status_code == 200

    m = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert m is not None
    csrf_token = m.group(1)

    response = client.post(
        "/login",
        data={
            "identifier": "demo",
            "password": "secret123",
            "next": "/",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    _reset_settings_and_db()

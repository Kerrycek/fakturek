from __future__ import annotations
from fakturek.time_utils import utc_now

from datetime import timedelta

import pytest
from starlette.testclient import TestClient

import fakturek.db as db_module
from fakturek.api_tokens import create_api_token
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings


sqlalchemy = pytest.importorskip("sqlalchemy")


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(
    monkeypatch,
    tmp_path,
    *,
    api_rate_limit_max: int = 240,
    api_monthly_quota_max: int = 2500,
    csrf_enabled: int = 0,
):
    db_path = tmp_path / "phase57.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", str(csrf_enabled))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("API_RATE_LIMIT_MAX", str(api_rate_limit_max))
    monkeypatch.setenv("API_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("API_MONTHLY_QUOTA_MAX", str(api_monthly_quota_max))
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
                User(
                    id=1,
                    username="owner",
                    email="owner@example.test",
                    password_hash=hash_password("secret123", iterations=1_000),
                    is_active=True,
                ),
                Subject(
                    id=1,
                    name="Studio Alpha",
                    email="alpha@example.test",
                    public_username="studio-alpha",
                    city="Praha",
                    country="CZ",
                    default_currency="CZK",
                    ico="12345678",
                ),
                Subject(
                    id=2,
                    name="Studio Beta",
                    email="beta@example.test",
                    public_username="studio-beta",
                    city="Brno",
                    country="CZ",
                    default_currency="CZK",
                    ico="87654321",
                ),
                UserSubject(
                    user_id=1,
                    subject_id=1,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                UserSubject(
                    user_id=1,
                    subject_id=2,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
            ]
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _login(client: TestClient, identifier: str = "owner") -> None:
    response = client.post(
        "/login",
        data={
            "identifier": identifier,
            "password": "secret123",
            "next": "/settings#api-access",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_subject_scoped_token_only_sees_its_subject(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    with SessionLocal() as db:
        _row, token = create_api_token(db, user_id=1, subject_id=1, name="Alpha token")
        db.commit()

    headers = {"Authorization": f"Bearer {token}"}

    me_response = client.get("/api/v1/me", headers=headers)
    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["token"]["subject_id"] == 1
    assert me_payload["token"]["subject_name"] == "Studio Alpha"
    assert len(me_payload["subjects"]) == 1
    assert me_payload["subjects"][0]["id"] == 1

    list_response = client.get("/api/v1/subjects", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total_items"] == 1
    assert [item["id"] for item in list_payload["items"]] == [1]

    detail_ok = client.get("/api/v1/subjects/1", headers=headers)
    assert detail_ok.status_code == 200
    assert detail_ok.json()["name"] == "Studio Alpha"

    detail_denied = client.get("/api/v1/subjects/2", headers=headers)
    assert detail_denied.status_code == 403
    assert detail_denied.json()["error"]["code"] == "subject_access_denied"
    assert detail_denied.json()["error"]["details"]["token_scope"] is True

    _reset_settings_and_db()


def test_revoke_expiry_and_last_used_continue_to_work(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    with SessionLocal() as db:
        row, token = create_api_token(db, user_id=1, subject_id=1, name="Alpha token")
        expired_row, expired_token = create_api_token(
            db,
            user_id=1,
            subject_id=1,
            name="Expired token",
            expires_at=utc_now() - timedelta(minutes=5),
        )
        token_id = int(row.id)
        expired_token_id = int(expired_row.id)
        db.commit()

    ok_headers = {"Authorization": f"Bearer {token}"}
    expired_headers = {"Authorization": f"Bearer {expired_token}"}

    ok_response = client.get("/api/v1/me", headers=ok_headers)
    assert ok_response.status_code == 200

    with SessionLocal() as db:
        from fakturek.models import ApiToken

        token_row = db.get(ApiToken, token_id)
        assert token_row is not None
        assert token_row.last_used_at is not None

        expired_row = db.get(ApiToken, expired_token_id)
        assert expired_row is not None

    expired_response = client.get("/api/v1/me", headers=expired_headers)
    assert expired_response.status_code == 401
    assert expired_response.json()["error"]["code"] == "auth_expired_token"

    with SessionLocal() as db:
        from fakturek.models import ApiToken

        token_row = db.get(ApiToken, token_id)
        assert token_row is not None
        token_row.revoked_at = utc_now()
        db.commit()

    revoked_response = client.get("/api/v1/me", headers=ok_headers)
    assert revoked_response.status_code == 401
    assert revoked_response.json()["error"]["code"] == "auth_revoked_token"

    _reset_settings_and_db()


def test_api_rate_limit_is_per_token(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path, api_rate_limit_max=2)

    with SessionLocal() as db:
        _row_a, token_a = create_api_token(db, user_id=1, subject_id=1, name="Alpha token")
        _row_b, token_b = create_api_token(db, user_id=1, subject_id=1, name="Second token")
        db.commit()

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    first = client.get("/api/v1/me", headers=headers_a)
    second = client.get("/api/v1/me", headers=headers_a)
    third = client.get("/api/v1/me", headers=headers_a)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "api_rate_limited"
    assert third.headers["Retry-After"]
    assert third.headers["X-RateLimit-Limit"] == "2"

    unaffected = client.get("/api/v1/me", headers=headers_b)
    assert unaffected.status_code == 200

    _reset_settings_and_db()


def test_api_monthly_quota_is_per_token(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(
        monkeypatch,
        tmp_path,
        api_monthly_quota_max=2,
    )

    with SessionLocal() as db:
        _row_a, token_a = create_api_token(db, user_id=1, subject_id=1, name="Alpha token")
        _row_b, token_b = create_api_token(db, user_id=1, subject_id=1, name="Second token")
        db.commit()

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    first = client.get("/api/v1/me", headers=headers_a)
    second = client.get("/api/v1/me", headers=headers_a)
    third = client.get("/api/v1/me", headers=headers_a)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "api_monthly_quota_exceeded"
    assert third.headers["Retry-After"]
    assert third.headers["X-RateLimit-Monthly-Limit"] == "2"
    assert third.headers["X-RateLimit-Monthly-Remaining"] == "0"

    unaffected = client.get("/api/v1/me", headers=headers_b)
    assert unaffected.status_code == 200

    _reset_settings_and_db()


def test_settings_api_token_form_requires_subject_and_stores_scope(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    invalid = client.post(
        "/settings/api-tokens/create",
        data={
            "name": "Scoped token",
            "expires_in_days": "30",
            "subject_id": "",
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 400
    assert "Vyber prosím konkrétní subjekt" in invalid.text
    assert "Studio Alpha • IČO 12345678" in invalid.text
    assert "Studio Beta • IČO 87654321" in invalid.text

    valid = client.post(
        "/settings/api-tokens/create",
        data={
            "name": "Scoped token",
            "expires_in_days": "30",
            "subject_id": "2",
            "is_sandbox": "1",
        },
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert valid.headers["location"] == "/settings?saved=1#api-access"

    with SessionLocal() as db:
        from fakturek.models import ApiToken

        row = db.query(ApiToken).filter(ApiToken.user_id == 1, ApiToken.name == "Scoped token").one()
        assert row.subject_id == 2
        assert row.is_sandbox is True

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Scope: Studio Beta • IČO 87654321" in settings_page.text
    assert "Režim: zkušební" in settings_page.text
    assert "2500 / měsíc" in settings_page.text

    _reset_settings_and_db()

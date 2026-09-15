from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from contextlib import closing
from datetime import timedelta

import pytest
from starlette.testclient import TestClient
import uvicorn

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
import fakturek.security as security_module
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.security import decrypt_secret, encrypt_secret
from fakturek.settings import get_settings
from fakturek.time_utils import utc_now


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _set_valid_production_environment(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DEBUG", "0")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "session-" + "a" * 48)
    monkeypatch.setenv("SIGNUP_TOKEN_KEY", "signup-" + "b" * 48)
    monkeypatch.setenv("PUBLIC_LINK_HMAC_KEY", "public-" + "c" * 48)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "encrypt-" + "d" * 48)
    monkeypatch.setenv("INTERNAL_JOB_TOKEN", "jobs-" + "e" * 48)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://invoices.example.test")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")


def _setup_sqlite_app(monkeypatch, tmp_path, *, login_rate_limit_max: int = 10):
    db_path = tmp_path / "security.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("INTERNAL_JOB_TOKEN", "job-secret")
    monkeypatch.setenv("LOGIN_RATE_LIMIT_MAX", str(login_rate_limit_max))
    monkeypatch.setenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(Subject(id=1, name="Seller One s.r.o.", email="seller@example.test"))
        db.add(Subject(id=2, name="Seller Two s.r.o.", email="seller2@example.test"))
        db.add(
            User(
                id=1,
                username="owner",
                email="owner@example.test",
                password_hash=hash_password("secret123", iterations=1_000),
                is_active=True,
            )
        )
        db.add(
            UserSubject(
                id=1,
                user_id=1,
                subject_id=1,
                role="owner",
                can_view=True,
                can_edit=True,
                can_issue=True,
            )
        )
        db.add(
            UserSubject(
                id=2,
                user_id=1,
                subject_id=2,
                role="owner",
                can_view=True,
                can_edit=True,
                can_issue=True,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _login(client: TestClient, *, password: str = "secret123") -> None:
    login_page = client.get("/login")
    assert login_page.status_code == 200
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={
            "identifier": "owner",
            "password": password,
            "next": "/",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


class _UvicornThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(config)

    def run(self) -> None:  # pragma: no cover - exercised by integration test below
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def test_internal_jobs_require_explicit_token(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    denied = client.post("/internal/jobs/bank-sync")
    assert denied.status_code == 403

    allowed = client.post(
        "/internal/jobs/bank-sync",
        headers={"X-Internal-Job-Token": "job-secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"

    _reset_settings_and_db()


def test_browser_route_inventory_is_hidden_but_api_docs_remain_public(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    root_paths = {str(getattr(route, "path", "")) for route in client.app.routes}
    assert "/docs" not in root_paths
    assert "/redoc" not in root_paths
    assert "/openapi.json" not in root_paths
    assert client.get("/api/v1/docs").status_code == 200
    assert client.get("/api/v1/openapi.json").status_code == 200

    _reset_settings_and_db()


def test_public_signup_can_create_and_verify_a_free_account(monkeypatch, tmp_path):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Fakturek")
    sent_messages = []

    def _capture_email(_config, message):
        sent_messages.append(message)
        return "test-message-id", "sent"

    monkeypatch.setattr("fakturek.main.send_via_smtp", _capture_email)
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    signup_page = client.get("/signup")
    assert signup_page.status_code == 200
    csrf_token = _extract_csrf(signup_page.text)
    response = client.post(
        "/signup",
        data={
            "csrf_token": csrf_token,
            "next": "/",
            "username": "new-owner",
            "email": "new-owner@example.test",
            "password": "correct-horse-battery-staple",  # pragma: allowlist secret
            "password2": "correct-horse-battery-staple",  # pragma: allowlist secret
            "subject_name": "New Company s.r.o.",
            "subject_email": "billing@example.test",
            "subject_country": "CZ",
            "subject_default_currency": "CZK",
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    pending_url = response.headers["location"]
    assert pending_url.startswith("/signup/pending?")
    assert "new-owner@example.test" not in pending_url
    pending = client.get(pending_url)
    assert pending.status_code == 200
    assert "Zkontroluj e-mail" in pending.text
    assert "ne•••••••@example.test" in pending.text
    assert len(sent_messages) == 1

    from sqlalchemy import select

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "new-owner"))
        assert user is not None
        assert user.is_active is False
        assert user.email_verified_at is None

    body = sent_messages[0].get_body(preferencelist=("plain",)).get_content()
    verify_match = re.search(r"https?://[^\s]+(/signup/verify\?token=[^\s]+)", body)
    assert verify_match is not None
    verified = client.get(verify_match.group(1), follow_redirects=False)
    assert verified.status_code == 303
    assert "/login?verified=1" in verified.headers["location"]

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "new-owner"))
        assert user is not None
        assert user.is_active is True
        assert user.email_verified_at is not None

        user.is_active = False
        db.commit()

    reused_after_deactivation = client.get(
        verify_match.group(1),
        follow_redirects=False,
    )
    assert reused_after_deactivation.status_code == 403
    assert "Aktivační odkaz ho nemůže znovu zapnout" in reused_after_deactivation.text

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "new-owner"))
        assert user is not None
        assert user.is_active is False

    _reset_settings_and_db()


def test_legacy_failed_login_lock_cannot_be_used_to_deny_account_access(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        user.failed_login_count = 3
        user.failed_login_locked_until = utc_now() + timedelta(hours=1)
        db.commit()

    _login(client)

    with SessionLocal() as db:
        user = db.get(User, 1)
        assert user is not None
        assert user.failed_login_count == 0
        assert user.failed_login_locked_until is None

    _reset_settings_and_db()


def test_authenticated_request_fails_closed_when_session_validation_db_is_unavailable(
    monkeypatch,
    tmp_path,
):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)
    original_get_sessionmaker = db_module.get_sessionmaker

    calls = 0

    def unavailable_during_session_validation():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("database-password=must-not-leak")
        return original_get_sessionmaker()

    # RBAC opens the first session; the authentication middleware performs the
    # second lookup that validates the signed session against the current user.
    monkeypatch.setattr(
        db_module,
        "get_sessionmaker",
        unavailable_during_session_validation,
    )
    response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {
        "detail": "Session validation is temporarily unavailable"
    }
    assert "database-password" not in response.text

    monkeypatch.setattr(db_module, "get_sessionmaker", original_get_sessionmaker)
    assert client.get("/").status_code == 200
    _reset_settings_and_db()


def test_authenticated_request_fails_closed_when_authorization_db_query_fails(
    monkeypatch,
    tmp_path,
):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    from sqlalchemy.orm import Session

    original_scalar = Session.scalar

    def unavailable_during_authorization(_session, _statement, *args, **kwargs):
        raise RuntimeError("database-password=must-not-leak")

    monkeypatch.setattr(Session, "scalar", unavailable_during_authorization)
    response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {
        "detail": "Authorization validation is temporarily unavailable"
    }
    assert "database-password" not in response.text

    monkeypatch.setattr(Session, "scalar", original_scalar)
    assert client.get("/").status_code == 200
    _reset_settings_and_db()


def test_logout_and_theme_posts_require_csrf_when_enabled(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    logout_denied = client.post("/logout", follow_redirects=False)
    assert logout_denied.status_code == 403

    home = client.get("/")
    csrf_token = _extract_csrf(home.text)
    logout_allowed = client.post(
        "/logout",
        data={"csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert logout_allowed.status_code == 303

    _login(client)
    settings_page = client.get("/settings")
    csrf_token = _extract_csrf(settings_page.text)
    theme_denied = client.post("/settings/theme", data={"ui_theme": "dark"}, follow_redirects=False)
    assert theme_denied.status_code == 403

    theme_allowed = client.post(
        "/settings/theme",
        data={"ui_theme": "dark", "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert theme_allowed.status_code == 303

    _reset_settings_and_db()


@pytest.mark.parametrize(
    "unsafe_next",
    (
        "//attacker.example.test/steal",
        "/\\attacker.example.test/steal",
        "/safe\r\nLocation: https://attacker.example.test/steal",
    ),
)
def test_login_rejects_unsafe_redirect_targets(monkeypatch, tmp_path, unsafe_next):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    login_page = client.get("/login", params={"next": unsafe_next})
    csrf_token = _extract_csrf(login_page.text)

    response = client.post(
        "/login",
        data={
            "identifier": "owner",
            "password": "secret123",
            "next": unsafe_next,
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    _reset_settings_and_db()


def test_subject_switch_requires_signed_token_when_auth_is_enabled(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    home = client.get("/")
    assert home.status_code == 200
    form_match = re.search(
        r'(<form[^>]+method="post"[^>]+action="(/subjects/2/switch)"[^>]*>.*?</form>)',
        home.text,
        re.S,
    )
    assert form_match is not None
    switch_form = form_match.group(1)
    action_match = re.search(r'action="([^"]+)"', switch_form)
    assert action_match is not None
    st_match = re.search(r'name="st" value="([^"]+)"', switch_form)
    assert st_match is not None
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', switch_form)
    assert csrf_match is not None
    next_match = re.search(r'name="next" value="([^"]+)"', switch_form)
    assert next_match is not None

    switch_action = action_match.group(1)
    switch_token = st_match.group(1)
    csrf_token = csrf_match.group(1)
    next_value = next_match.group(1)

    denied = client.post(
        action_match.group(1),
        data={"next": next_value, "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert denied.status_code == 403

    allowed = client.post(
        switch_action,
        data={"next": next_value, "st": switch_token, "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert allowed.headers["location"] == "/"

    _reset_settings_and_db()


def test_login_rate_limit_ignores_untrusted_forwarded_for_header(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path, login_rate_limit_max=1)

    login_page = client.get("/login")
    assert login_page.status_code == 200
    csrf_token = _extract_csrf(login_page.text)

    first = client.post(
        "/login",
        data={
            "identifier": "owner",
            "password": "wrong-password",
            "next": "/",
            "csrf_token": csrf_token,
        },
        headers={"X-Forwarded-For": "198.51.100.10", "X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert first.status_code == 401

    second = client.post(
        "/login",
        data={
            "identifier": "owner",
            "password": "wrong-password",
            "next": "/",
            "csrf_token": csrf_token,
        },
        headers={"X-Forwarded-For": "198.51.100.11", "X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert second.status_code == 429

    _reset_settings_and_db()


def test_unknown_login_still_runs_password_verification(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    calls: list[str] = []

    def _fake_verify(_password: str, stored_hash: str) -> bool:
        calls.append(stored_hash)
        return False

    monkeypatch.setattr("fakturek.main.verify_password", _fake_verify)
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={
            "identifier": "missing-user",
            "password": "wrong-password",
            "next": "/",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert calls == [""]
    _reset_settings_and_db()


def test_malformed_host_keeps_canonical_login_next(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    port = _free_port()
    server = _UvicornThread(client.app, port)
    server.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("Timed out waiting for local uvicorn server")

    try:
        completed = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-D",
                "-",
                "-H",
                "Host: foo?",
                f"http://127.0.0.1:{port}/admin",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.stop()
        server.join(timeout=5)

    response = str(completed.stdout or "")
    assert "303 See Other" in response
    assert "location: /login?next=/admin" in response.lower()
    assert "/%3f/admin" not in response.lower()
    _reset_settings_and_db()


def test_secret_encryption_fails_closed_without_cryptography(monkeypatch):
    monkeypatch.setattr(security_module, "Fernet", None)

    with pytest.raises(RuntimeError, match="cryptography is required"):
        encrypt_secret("sensitive", secret_key="test-key", purpose="test")


def test_secret_encryption_uses_requested_key():
    encrypted = encrypt_secret("sensitive", secret_key="key-a", purpose="test")

    assert encrypted is not None
    assert encrypted != "sensitive"
    assert decrypt_secret(encrypted, secret_key="key-a", purpose="test") == "sensitive"
    assert decrypt_secret(encrypted, secret_key="key-b", purpose="test") is None


def test_unknown_environment_cannot_fall_back_to_development_defaults(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="APP_ENV must be one of"):
        get_settings()

    get_settings.cache_clear()


def test_production_settings_allow_setup_token_removal(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.delenv("SETUP_TOKEN", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.setup_token is None
    get_settings.cache_clear()


def test_production_settings_reject_duplicate_security_keys(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "session-" + "a" * 48)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="must be different"):
        get_settings()

    get_settings.cache_clear()


def test_production_settings_reject_insecure_integration_url(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("ARES_BASE_URL", "http://ares.example.test/api")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="HTTPS"):
        get_settings()

    get_settings.cache_clear()


@pytest.mark.parametrize(
    "value",
    (
        "https://app.example.test:invalid",
        "https://app.example.test\\malformed",
        "https://app.example.test/path",
    ),
)
def test_production_settings_reject_malformed_canonical_origin(monkeypatch, value):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", value)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError):
        get_settings()

    get_settings.cache_clear()


def test_production_settings_require_canonical_origins(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("APP_BASE_URL", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="canonical HTTPS origin"):
        get_settings()

    get_settings.cache_clear()


def test_production_error_page_does_not_leak_exception_or_log_path(monkeypatch, tmp_path):
    db_path = tmp_path / "errors.sqlite3"
    log_dir = tmp_path / "private-logs"
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("FAKTUREK_VERBOSE_ERRORS", "0")
    monkeypatch.setenv("FAKTUREK_LOG_DIR", str(log_dir))
    _reset_settings_and_db()

    from fakturek.main import create_app

    app = create_app()

    api_app = next(route.app for route in app.routes if getattr(route, "path", None) == "/api/v1")

    @api_app.get("/__security-test-boom")
    def _security_test_boom():
        raise RuntimeError("database-password=do-not-leak")

    @app.get("/internal/jobs/__security-test-boom")
    def _root_security_test_boom():
        raise RuntimeError("database-password=do-not-leak")

    client = TestClient(app, base_url="https://app.example.test", raise_server_exceptions=False)
    response = client.get(
        "/api/v1/__security-test-boom",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 500
    assert "Internal Server Error" in response.text
    assert "database-password" not in response.text
    assert "RuntimeError" not in response.text
    assert str(log_dir) not in response.text
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000"

    html_response = client.get(
        "/internal/jobs/__security-test-boom",
        headers={"Accept": "text/html"},
    )
    assert html_response.status_code == 500
    assert "database-password" not in html_response.text
    assert "RuntimeError" not in html_response.text
    assert html_response.headers["X-Request-ID"]
    assert html_response.headers["X-Content-Type-Options"] == "nosniff"
    assert html_response.headers["X-Frame-Options"] == "DENY"
    assert html_response.headers["Strict-Transport-Security"] == "max-age=63072000"
    assert "frame-ancestors 'none'" in html_response.headers["Content-Security-Policy"]
    _reset_settings_and_db()


def test_rejected_host_still_receives_security_headers(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted-host.sqlite3"
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    _reset_settings_and_db()

    from fakturek.main import create_app

    client = TestClient(
        create_app(),
        base_url="https://attacker.example.test",
        raise_server_exceptions=False,
    )
    response = client.get("/healthz")

    assert response.status_code == 400
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000"
    _reset_settings_and_db()


def test_https_development_response_receives_hsts(monkeypatch, tmp_path):
    db_path = tmp_path / "dev-https-headers.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.main import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000"
    _reset_settings_and_db()


def test_setup_requires_twelve_character_password(monkeypatch, tmp_path):
    db_path = tmp_path / "setup.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("SETUP_TOKEN", "setup-token")
    _reset_settings_and_db()

    from fakturek.db import get_engine
    from fakturek.main import create_app

    Base.metadata.create_all(get_engine())
    client = TestClient(create_app())
    setup_page = client.get("/setup")
    assert 'id="token" name="token" type="password" value=""' in setup_page.text
    csrf_token = _extract_csrf(setup_page.text)

    response = client.post(
        "/setup",
        data={
            "token": "setup-token",
            "username": "owner",
            "email": "owner@example.test",
            "password": "short-pass",
            "password2": "short-pass",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "alespoň 12 znaků" in response.text
    assert "setup-token" not in response.text
    _reset_settings_and_db()

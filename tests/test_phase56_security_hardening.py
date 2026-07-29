from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from contextlib import closing

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
    _reset_settings_and_db()

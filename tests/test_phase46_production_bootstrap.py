from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from fakturek.main import create_app
from fakturek.settings import get_settings


SEED_ENV_NAMES = [
    "FAKTUREK_BOOTSTRAP_USERNAME",
    "FAKTUREK_BOOTSTRAP_EMAIL",
    "FAKTUREK_BOOTSTRAP_PASSWORD",
    "FAKTUREK_BOOTSTRAP_SUBJECT_ID",
    "FAKTUREK_BOOTSTRAP_SUBJECT_NAME",
    "FAKTUREK_BOOTSTRAP_SUBJECT_EMAIL",
    "FAKTUREK_BOOTSTRAP_SUBJECT_PHONE",
    "FAKTUREK_BOOTSTRAP_SUBJECT_STREET",
    "FAKTUREK_BOOTSTRAP_SUBJECT_CITY",
    "FAKTUREK_BOOTSTRAP_SUBJECT_ZIP",
    "FAKTUREK_BOOTSTRAP_SUBJECT_COUNTRY",
    "FAKTUREK_BOOTSTRAP_SUBJECT_ICO",
    "FAKTUREK_BOOTSTRAP_SUBJECT_DIC",
    "FAKTUREK_BOOTSTRAP_SUBJECT_DEFAULT_CURRENCY",
    "FAKTUREK_BOOTSTRAP_SUBJECT_IS_VAT_PAYER",
    "FAKTUREK_BOOTSTRAP_SUBJECT_PUBLIC_USERNAME",
    "FAKTUREK_BOOTSTRAP_ADOPT_LEGACY_DEMO",
    "FAKTUREK_BOOTSTRAP_PRINT_PASSWORD",
    "FAKTUREK_BOOTSTRAP_CREDENTIALS_FILE",
    "FAKTUREK_SHOW_BOOTSTRAP_CREDS",
]


def _reset_settings() -> None:
    get_settings.cache_clear()


def _load_seed_user_module():
    module_name = "phase46_seed_user"
    path = Path(__file__).resolve().parents[1] / "tools" / "seed_user.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_login_page_hides_bootstrap_credentials_by_default(monkeypatch):
    for name in SEED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    _reset_settings()

    client = TestClient(create_app())
    response = client.get("/login")

    assert response.status_code == 200
    assert "Počáteční přihlašovací údaje" not in response.text

    _reset_settings()


def test_login_page_can_show_bootstrap_credentials_when_enabled(monkeypatch):
    for name in SEED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAKTUREK_SHOW_BOOTSTRAP_CREDS", "1")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_USERNAME", "KerryCZE")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_PASSWORD", "BootstrapPass123!")
    _reset_settings()

    client = TestClient(create_app())
    response = client.get("/login")

    assert response.status_code == 200
    assert "Počáteční přihlašovací údaje" in response.text
    assert "KerryCZE" in response.text
    assert "BootstrapPass123!" in response.text

    _reset_settings()


def test_login_page_never_shows_bootstrap_credentials_in_prod(monkeypatch):
    for name in SEED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DEBUG", "0")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "session-" + "a" * 48)
    monkeypatch.setenv("SIGNUP_TOKEN_KEY", "signup-" + "b" * 48)
    monkeypatch.setenv("PUBLIC_LINK_HMAC_KEY", "public-" + "c" * 48)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "encrypt-" + "d" * 48)
    monkeypatch.setenv("INTERNAL_JOB_TOKEN", "jobs-" + "e" * 48)
    monkeypatch.setenv("SETUP_TOKEN", "setup-" + "f" * 48)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://invoices.example.test")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("FAKTUREK_SHOW_BOOTSTRAP_CREDS", "1")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_USERNAME", "KerryCZE")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_PASSWORD", "BootstrapPass123!")
    _reset_settings()

    client = TestClient(create_app(), base_url="https://app.example.test")
    response = client.get("/login")

    assert response.status_code == 200
    assert "Počáteční přihlašovací údaje" not in response.text
    assert "BootstrapPass123!" not in response.text

    _reset_settings()


def test_seed_user_defaults_target_owner_and_subject(monkeypatch):
    for name in SEED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    seed_user = _load_seed_user_module()
    cfg = seed_user.load_config()

    assert cfg.username == "owner"
    assert cfg.email == "owner@example.com"
    assert cfg.subject_name == "Moje firma s.r.o."
    assert cfg.subject_street == ""
    assert cfg.subject_city == ""
    assert cfg.subject_zip == ""
    assert cfg.subject_country == "CZ"
    assert cfg.subject_ico == ""
    assert cfg.subject_phone == ""
    assert cfg.subject_default_currency == "CZK"
    assert cfg.subject_public_username == "moje-firma"
    assert cfg.adopt_legacy_demo is True
    assert cfg.print_password is False
    assert isinstance(cfg.password, str) and len(cfg.password) >= 10


def test_seed_user_requires_explicit_identity_in_prod(monkeypatch):
    for name in SEED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "prod")

    seed_user = _load_seed_user_module()

    try:
        seed_user.load_config()
        assert False, "expected load_config() to require explicit prod seed identity"
    except ValueError as exc:
        assert "FAKTUREK_BOOTSTRAP_USERNAME" in str(exc) or "FAKTUREK_BOOTSTRAP_EMAIL" in str(exc)

from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "tax-limits.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    current_year = date.today().year
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Demo Subject s.r.o.",
                    email="demo@example.test",
                    country="CZ",
                    default_currency="CZK",
                    tax_regime="standard",
                    flat_tax_band="1",
                    flat_tax_income_profile="general",
                ),
                Contact(
                    id=1,
                    subject_id=1,
                    name="Client a.s.",
                    email="billing@client.example.test",
                    country="CZ",
                ),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0001",
                    status="sent",
                    issue_date=date(current_year, 2, 10),
                    due_date=date(current_year, 2, 20),
                    currency="CZK",
                    total_cents=1_200_000_00,
                ),
            ]
        )
        db.commit()

    return TestClient(create_app(), base_url="https://app.example.test"), SessionLocal


def test_settings_issuer_can_store_flat_tax_preferences(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/settings/issuer",
        data={
            "name": "Demo Subject s.r.o.",
            "email": "demo@example.test",
            "phone": "",
            "street": "",
            "city": "Praha",
            "zip": "11000",
            "country": "CZ",
            "ico": "12345678",
            "dic": "",
            "is_vat_payer": "",
            "tax_regime": "flat",
            "flat_tax_band": "1",
            "flat_tax_income_profile": "mostly_80_60",
            "tax_alerts_enabled": "1",
            "tax_alert_email": "alerts@example.test",
            "default_currency": "CZK",
            "default_invoice_footer_mode": "commercial_register",
            "default_invoice_footer_text": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    from fakturek.models import Subject

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        assert subject.tax_regime == "flat"
        assert subject.flat_tax_band == "1"
        assert subject.flat_tax_income_profile == "mostly_80_60"
        assert subject.tax_alerts_enabled is True

    refreshed = client.get("/settings")
    assert refreshed.status_code == 200

    _reset_settings_and_db()


def test_settings_rejects_flat_tax_for_vat_payer(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/settings/issuer",
        data={
            "name": "Demo Subject s.r.o.",
            "email": "demo@example.test",
            "phone": "",
            "street": "",
            "city": "Praha",
            "zip": "11000",
            "country": "CZ",
            "ico": "12345678",
            "dic": "CZ12345678",
            "is_vat_payer": "1",
            "tax_regime": "flat",
            "flat_tax_band": "1",
            "flat_tax_income_profile": "mostly_80_60",
            "default_currency": "CZK",
            "default_invoice_footer_mode": "commercial_register",
            "default_invoice_footer_text": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Plátce DPH nemůže být současně v paušálním režimu." in response.text

    _reset_settings_and_db()


def test_home_shows_flat_tax_thresholds_and_hides_vat_for_payer(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Subject

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        subject.tax_regime = "flat"
        subject.flat_tax_band = "1"
        subject.flat_tax_income_profile = "mostly_80_60"
        db.add(subject)
        db.commit()

    response = client.get("/")
    assert response.status_code == 200
    assert "Paušální režim" in response.text
    assert "1. pásmo" in response.text
    assert "1 500 000,00 CZK" in response.text
    assert "300 000,00 CZK" in response.text
    assert "Limit DPH" in response.text

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        subject.is_vat_payer = True
        subject.tax_regime = "standard"
        db.add(subject)
        db.commit()

    payer_response = client.get("/")
    assert payer_response.status_code == 200
    assert "Limit DPH" not in payer_response.text

    _reset_settings_and_db()


def test_home_sends_vat_limit_alert_email_once_per_stage(monkeypatch, tmp_path):
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "0")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setenv("INTERNAL_JOB_TOKEN", "job-token")

    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    import fakturek.main as main_module
    from fakturek.models import Invoice, Subject

    sent_messages: list[dict[str, str]] = []

    def _fake_send(cfg, msg):
        sent_messages.append(
            {
                "to": str(msg.get("To") or ""),
                "subject": str(msg.get("Subject") or ""),
                "body": msg.get_body(preferencelist=("plain",)).get_content(),
            }
        )
        return "<tax-alert@example.test>", "sent"

    monkeypatch.setattr(main_module, "send_via_smtp", _fake_send)

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        invoice = db.get(Invoice, 1)
        assert subject is not None
        assert invoice is not None
        subject.tax_alerts_enabled = True
        subject.tax_alert_email = "alerts@example.test"
        invoice.total_cents = 1_650_000_00
        db.add(subject)
        db.add(invoice)
        db.commit()

    first_response = client.post(
        "/internal/jobs/tax-alerts",
        headers={"X-Internal-Job-Token": "job-token"},
    )
    assert first_response.status_code == 200
    assert len(sent_messages) == 1
    assert sent_messages[0]["to"] == "alerts@example.test"
    assert "Limit DPH" in sent_messages[0]["subject"]
    assert "80 % limitu" in sent_messages[0]["body"]

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        assert subject.vat_alert_last_stage == 1
        assert subject.vat_alert_last_year == date.today().year

    second_response = client.post(
        "/internal/jobs/tax-alerts",
        headers={"X-Internal-Job-Token": "job-token"},
    )
    assert second_response.status_code == 200
    assert len(sent_messages) == 1

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        invoice.total_cents = 1_850_000_00
        db.add(invoice)
        db.commit()

    third_response = client.post(
        "/internal/jobs/tax-alerts",
        headers={"X-Internal-Job-Token": "job-token"},
    )
    assert third_response.status_code == 200
    assert len(sent_messages) == 2
    assert "90 % limitu" in sent_messages[1]["body"]

    _reset_settings_and_db()

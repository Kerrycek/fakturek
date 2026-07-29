from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings
from fakturek.public_links import build_public_invoice_urls


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path, *, public_base_url: str = ""):
    db_path = tmp_path / "phase39.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@fakturek.cz")
    monkeypatch.setenv("SMTP_FROM_NAME", "Fakturek.cz")
    if public_base_url:
        monkeypatch.setenv("PUBLIC_BASE_URL", public_base_url)
    else:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Demo Seller s.r.o.",
                email="owner@example.test",
                public_username="demo-seller",
                city="Praha",
                country="CZ",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Acme Client a.s.",
                email="billing@example.test, finance@example.test",
                city="Praha",
                country="CZ",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2026-0002",
                status="issued",
                issue_date=date(2026, 2, 5),
                due_date=date(2026, 2, 10),
                currency="CZK",
                total_cents=2500000,
                buyer_name_cache="Acme Client a.s.",
                public_token="tok-phase39",
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def test_public_links_use_short_canonical_path_without_external_port(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "testclient")
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    detail = client.get(
        "/invoices/1",
        headers={
            "host": "invoice.example:8443",
            "x-forwarded-proto": "https",
        },
    )
    assert detail.status_code == 200
    assert "https://invoice.example/i/" in detail.text
    assert ":8443" not in detail.text
    assert "/demo-seller/i/tok-phase39/2026-0002" not in detail.text

    public_path = build_public_invoice_urls(
        public_username="demo-seller",
        token="tok-phase39",
        invoice_number="2026-0002",
        invoice_id=1,
        secret_key="test-secret",
    )["view"]
    public_preview = client.get(public_path)
    assert public_preview.status_code == 200
    assert "Otevřít PDF" in public_preview.text

    _reset_settings_and_db()


def test_invoice_email_form_supports_cc_and_contact_shortcuts(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path, public_base_url="https://invoice.example")

    import fakturek.main as main_module
    from fakturek.models import Invoice, InvoiceEmail

    sent_messages = []

    def _fake_send(_cfg, msg):
        sent_messages.append(msg)
        return "<msg-1@example.test>", "sent"

    monkeypatch.setattr(main_module, "send_via_smtp", _fake_send)

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Poslat e-mailem" in detail.text
    assert "Kopie (CC)" in detail.text
    assert "billing@example.test" in detail.text
    assert "finance@example.test" in detail.text
    assert "Kopie mně" in detail.text
    assert 'data-fill-target="cc_email" data-fill-value="owner@example.test"' in detail.text
    assert "S pozdravem,\nDemo Seller s.r.o." in detail.text
    assert "Veřejný odkaz:" not in detail.text

    response = client.post(
        "/invoices/1/email",
        data={
            "to_email": "other@example.test",
            "cc_email": "copy@example.test",
            "subject": "Faktura 2026-0002",
            "body": "",
            "attach_pdf": "",
            "include_public_link": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/1?notice=")

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert str(msg["To"]) == "other@example.test"
    assert str(msg["Cc"]) == "copy@example.test"
    assert str(msg["From"]) == '"Fakturek.cz" <noreply@fakturek.cz>'
    body_text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "S pozdravem,\nDemo Seller s.r.o." in body_text
    assert "Veřejný odkaz:" not in body_text

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        email_row = db.query(InvoiceEmail).order_by(InvoiceEmail.id.desc()).first()
        assert invoice is not None
        assert invoice.status == "sent"
        assert invoice.sent_at is not None
        assert email_row is not None
        assert "To: other@example.test" in email_row.to_email
        assert "Cc: copy@example.test" in email_row.to_email
        assert email_row.status == "sent"

    _reset_settings_and_db()


def test_contact_accepts_multiple_emails_and_renders_mailto_links(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/contacts/new",
        data={
            "name": "Multi Mail s.r.o.",
            "email": "billing@example.test; finance@example.test",
            "phone": "",
            "street": "",
            "city": "Praha",
            "zip": "",
            "country": "CZ",
            "ico": "",
            "dic": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/contacts/")

    detail = client.get(location)
    assert detail.status_code == 200
    assert 'mailto:billing@example.test' in detail.text
    assert 'mailto:finance@example.test' in detail.text

    from fakturek.models import Contact

    with SessionLocal() as db:
        contact = db.query(Contact).filter(Contact.name == "Multi Mail s.r.o.").one()
        assert contact.email == "billing@example.test, finance@example.test"

    _reset_settings_and_db()

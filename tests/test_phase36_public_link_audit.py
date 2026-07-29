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
    db_path = tmp_path / "phase36-public-link-audit.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
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
                name="Test subject",
                email="owner@example.test",
                public_username=None,
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Importovaný klient",
                email="client@example.test",
                city="Praha",
                country="CZ",
                external_source="fakturoid",
                external_id="fakturoid-contact-1",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                number="2026-IMP-0001",
                status="issued",
                issue_date=date(2026, 3, 1),
                due_date=date(2026, 3, 15),
                currency="CZK",
                notes="Import z Fakturoidu",
                contact_id=1,
                buyer_name_cache="Importovaný klient",
                total_cents=12345,
                public_token=None,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def test_enable_public_link_for_imported_invoice_writes_audit_with_request(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "testclient")
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/1/public/enable",
        data={"next": "/invoices/1"},
        headers={
            "x-forwarded-for": "203.0.113.10",
            "user-agent": "pytest-phase36/1.0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    from fakturek.models import AuditLog, Invoice, Subject

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        subject = db.get(Subject, 1)
        audit_row = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "invoice", AuditLog.entity_id == 1)
            .order_by(AuditLog.id.desc())
            .first()
        )

        assert invoice is not None
        assert subject is not None
        assert audit_row is not None
        assert invoice.public_token
        assert subject.public_username
        assert audit_row.action == "invoice_public_enabled"
        assert audit_row.ip == "203.0.113.10"
        assert audit_row.user_agent == "pytest-phase36/1.0"

    _reset_settings_and_db()


def test_draft_invoice_hides_public_link_actions(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, Subject

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        subject = db.get(Subject, 1)
        assert invoice is not None
        assert subject is not None
        invoice.status = "draft"
        invoice.number = "DRAFT-1"
        invoice.public_token = "existing-token"
        subject.public_username = "test-subject"
        db.add_all([invoice, subject])
        db.commit()

    response = client.get("/invoices/1")

    assert response.status_code == 200
    body = response.text
    assert "Kopírovat odkaz" not in body
    assert "Sdílený odkaz" not in body
    assert "Zapnout veřejný odkaz" not in body
    assert 'href="/invoices/1/print"' in body

    _reset_settings_and_db()

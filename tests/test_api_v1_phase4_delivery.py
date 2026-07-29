from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

import fakturek.db as db_module
from fakturek.api_tokens import create_api_token
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings


sqlalchemy = pytest.importorskip("sqlalchemy")


PDF_BYTES = b"%PDF-1.4\n% phase4 api test\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_api_app(monkeypatch, tmp_path):
    db_path = tmp_path / "api-v1-phase4.sqlite3"
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    monkeypatch.setenv("PDF_STORAGE_DIR", str(pdf_dir))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "billing@example.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Studio Alpha")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, InvoiceItem, Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()

    with SessionLocal() as db:
        owner = User(
            id=1,
            username="owner",
            email="owner@example.test",
            password_hash=hash_password("pw", iterations=1000),
            is_active=True,
        )
        editor = User(
            id=2,
            username="editor",
            email="editor@example.test",
            password_hash=hash_password("pw", iterations=1000),
            is_active=True,
        )
        db.add_all([owner, editor])

        subject = Subject(
            id=1,
            name="Studio Alpha",
            email="billing@studio-alpha.test",
            city="Brno",
            street="Křižíkova 12",
            zip="60200",
            country="CZ",
            ico="12345678",
            dic="CZ12345678",
            default_currency="CZK",
            public_username="studio-alpha",
            is_vat_payer=True,
            tax_regime="standard",
            default_invoice_footer_mode="trade_register",
        )
        db.add(subject)

        db.add(
            UserSubject(
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
                user_id=2,
                subject_id=1,
                role="manager",
                can_view=True,
                can_edit=True,
                can_issue=False,
            )
        )

        contact = Contact(
            id=1,
            subject_id=1,
            name="Acme Client a.s.",
            email="ap@acme.test",
            city="Praha",
            country="CZ",
            ico="87654321",
        )
        db.add(contact)

        invoice = Invoice(
            id=1,
            subject_id=1,
            contact_id=1,
            number="2026-0001",
            status="issued",
            document_type="invoice",
            issue_date=date(2026, 3, 24),
            due_date=date(2026, 4, 7),
            currency="CZK",
            variable_symbol="20260001",
            total_cents=12_100,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            public_token=None,
            pdf_path=None,
        )
        db.add(invoice)
        db.add(
            InvoiceItem(
                id=1,
                invoice_id=1,
                description="Implementace API",
                quantity=Decimal("1.00"),
                unit="ks",
                unit_price_cents=10_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=10_000,
                line_vat_cents=2_100,
                line_total_cents=12_100,
                sort_order=1,
            )
        )

        _row_owner, owner_token = create_api_token(db, user_id=1, name="Owner tests")
        _row_editor, editor_token = create_api_token(db, user_id=2, name="Editor tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, owner_token, editor_token, pdf_dir


def test_api_v1_invoice_pdf_requires_export_scope(monkeypatch, tmp_path):
    client, SessionLocal, _owner_token, _editor_token, _pdf_dir = _setup_sqlite_api_app(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.api_tokens import create_api_token

        _row, read_only_token = create_api_token(
            db,
            user_id=1,
            subject_id=1,
            name="Read only no export",
            can_read=True,
            can_write=False,
            can_issue=False,
            can_export=False,
        )
        db.commit()

    response = client.get(
        "/api/v1/subjects/1/invoices/1/pdf?download=1",
        headers={"Authorization": f"Bearer {read_only_token}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "api_token_scope_denied"

    _reset_settings_and_db()


def test_api_v1_invoice_pdf_and_public_link_management(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, _editor_token, pdf_dir = _setup_sqlite_api_app(monkeypatch, tmp_path)
    monkeypatch.setattr("fakturek.api_v1.render_invoice_pdf_bytes", lambda _data: PDF_BYTES)
    headers = {"Authorization": f"Bearer {owner_token}"}

    pdf_response = client.get("/api/v1/subjects/1/invoices/1/pdf?download=1", headers=headers)
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    assert pdf_response.headers["content-disposition"].startswith("attachment;")
    assert pdf_response.content.startswith(b"%PDF")

    with SessionLocal() as db:
        from fakturek.models import Invoice

        invoice = db.query(Invoice).filter(Invoice.id == 1).one()
        assert invoice.pdf_path
        assert (pdf_dir / invoice.pdf_path).exists()

    ensure_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "public-link-ensure-1",
    }
    ensure_response = client.post(
        "/api/v1/subjects/1/invoices/1/public-link",
        headers=ensure_headers,
        json={"rotate": False},
    )
    assert ensure_response.status_code == 200
    ensured = ensure_response.json()
    assert ensured["enabled"] is True
    assert ensured["url"].startswith("https://billing.example.test/studio-alpha/i/")
    assert ensured["short_url"].startswith("https://billing.example.test/i/")
    assert ensured["pdf_url"].endswith("/pdf")
    assert ensured["isdoc_url"].endswith("/isdoc")
    assert ensured["isdoc_download_url"].endswith("/isdoc?download=1")
    old_short_url = ensured["short_url"]

    replay_response = client.post(
        "/api/v1/subjects/1/invoices/1/public-link",
        headers=ensure_headers,
        json={"rotate": False},
    )
    assert replay_response.status_code == 200
    assert replay_response.json()["short_url"] == old_short_url

    rotate_response = client.post(
        "/api/v1/subjects/1/invoices/1/public-link",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "public-link-rotate-1"},
        json={"rotate": True},
    )
    assert rotate_response.status_code == 200
    rotated = rotate_response.json()
    assert rotated["enabled"] is True
    assert rotated["short_url"] != old_short_url

    delete_response = client.delete(
        "/api/v1/subjects/1/invoices/1/public-link",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "public-link-delete-1"},
    )
    assert delete_response.status_code == 200
    deleted = delete_response.json()
    assert deleted == {
        "enabled": False,
        "url": None,
        "short_url": None,
        "pdf_url": None,
        "pdf_download_url": None,
        "isdoc_url": None,
        "isdoc_download_url": None,
    }


def test_api_v1_send_invoice_email_and_log_is_idempotent(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, _editor_token, _pdf_dir = _setup_sqlite_api_app(monkeypatch, tmp_path)
    monkeypatch.setattr("fakturek.api_v1.render_invoice_pdf_bytes", lambda _data: PDF_BYTES)

    smtp_calls: list[dict[str, object]] = []

    def _fake_send_via_smtp(cfg, msg):
        smtp_calls.append(
            {
                "host": cfg.host,
                "to": str(msg.get("To") or ""),
                "cc": str(msg.get("Cc") or ""),
                "subject": str(msg.get("Subject") or ""),
            }
        )
        return "<msg-1@example.test>", "sent"

    monkeypatch.setattr("fakturek.api_v1.send_via_smtp", _fake_send_via_smtp)

    headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "invoice-email-send-1",
    }
    payload = {
        "cc": "copy@example.test",
        "attach_pdf": True,
        "include_public_link": True,
    }
    send_response = client.post(
        "/api/v1/subjects/1/invoices/1/send-email",
        headers=headers,
        json=payload,
    )
    assert send_response.status_code == 200
    body = send_response.json()
    assert body["email"]["status"] == "sent"
    assert body["email"]["message_id"] == "<msg-1@example.test>"
    assert body["invoice_status"] == "sent"
    assert body["attached_pdf"] is True
    assert body["public_link_included"] is True
    assert len(smtp_calls) == 1
    assert smtp_calls[0]["to"] == "ap@acme.test"
    assert smtp_calls[0]["cc"] == "copy@example.test"

    replay_response = client.post(
        "/api/v1/subjects/1/invoices/1/send-email",
        headers=headers,
        json=payload,
    )
    assert replay_response.status_code == 200
    assert replay_response.json()["email"]["message_id"] == "<msg-1@example.test>"
    assert len(smtp_calls) == 1

    log_response = client.get(
        "/api/v1/subjects/1/invoices/1/emails",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert log_response.status_code == 200
    log_body = log_response.json()
    assert log_body["total_items"] == 1
    assert log_body["items"][0]["status"] == "sent"
    assert log_body["items"][0]["to_email"] == "To: ap@acme.test | Cc: copy@example.test"

    with SessionLocal() as db:
        from fakturek.models import Invoice, InvoiceEmail

        invoice = db.query(Invoice).filter(Invoice.id == 1).one()
        emails = db.query(InvoiceEmail).filter(InvoiceEmail.invoice_id == 1).all()
        assert invoice.status == "sent"
        assert invoice.sent_at is not None
        assert len(emails) == 1
        assert emails[0].message_id == "<msg-1@example.test>"


def test_api_v1_send_invoice_email_requires_issue_permission(monkeypatch, tmp_path):
    client, _SessionLocal, _owner_token, editor_token, _pdf_dir = _setup_sqlite_api_app(monkeypatch, tmp_path)
    monkeypatch.setattr("fakturek.api_v1.send_via_smtp", lambda cfg, msg: ("<ignored>", "sent"))
    monkeypatch.setattr("fakturek.api_v1.render_invoice_pdf_bytes", lambda _data: PDF_BYTES)

    response = client.post(
        "/api/v1/subjects/1/invoices/1/send-email",
        headers={"Authorization": f"Bearer {editor_token}"},
        json={"attach_pdf": False},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "subject_access_denied"

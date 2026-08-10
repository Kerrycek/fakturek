from __future__ import annotations


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


def _setup_sqlite_api_app(monkeypatch, tmp_path):
    db_path = tmp_path / "api-v1-phase3.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Subject, User, UserSubject

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
            fixed_variable_symbol="20260001",
        )
        db.add(contact)

        _row_owner, owner_token = create_api_token(db, user_id=1, name="Owner tests")
        _row_editor, editor_token = create_api_token(db, user_id=2, name="Editor tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, owner_token, editor_token


def test_api_v1_contact_create_patch_and_idempotency(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, _editor_token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "contact-create-1",
    }

    create_payload = {
        "name": "Beta Client s.r.o.",
        "email": "finance@beta.test; ap@beta.test",
        "phone": "123 456 789",
        "country": "sk",
        "fixed_variable_symbol": "VS 2026/0002",
    }
    create_response = client.post("/api/v1/subjects/1/contacts", headers=headers, json=create_payload)
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["name"] == "Beta Client s.r.o."
    assert created["email"] == "finance@beta.test, ap@beta.test"
    assert created["country"] == "SK"
    assert created["fixed_variable_symbol"] == "20260002"
    created_id = created["id"]

    replay_response = client.post("/api/v1/subjects/1/contacts", headers=headers, json=create_payload)
    assert replay_response.status_code == 201
    assert replay_response.json()["id"] == created_id

    mismatch_response = client.post(
        "/api/v1/subjects/1/contacts",
        headers=headers,
        json={**create_payload, "name": "Different"},
    )
    assert mismatch_response.status_code == 409
    assert mismatch_response.json()["error"]["code"] == "idempotency_key_reused"

    patch_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "contact-patch-1",
    }
    patch_payload = {
        "name": "Beta Client SE",
        "email": None,
        "fixed_variable_symbol": "",
    }
    patch_response = client.patch(
        f"/api/v1/subjects/1/contacts/{created_id}",
        headers=patch_headers,
        json=patch_payload,
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["name"] == "Beta Client SE"
    assert patched["email"] is None
    assert patched["fixed_variable_symbol"] is None

    patch_replay = client.patch(
        f"/api/v1/subjects/1/contacts/{created_id}",
        headers=patch_headers,
        json=patch_payload,
    )
    assert patch_replay.status_code == 200
    assert patch_replay.json()["name"] == "Beta Client SE"

    with SessionLocal() as db:
        from fakturek.models import Contact

        rows = db.query(Contact).filter(Contact.subject_id == 1).all()
        assert len(rows) == 2


def test_api_v1_create_patch_and_issue_invoice_with_permissions(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, editor_token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    owner_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "invoice-create-1",
    }
    create_payload = {
        "contact_id": 1,
        "issue_date": "2026-03-24",
        "due_date": "2026-04-07",
        "document_type": "invoice",
        "language": "en",
        "style": "modern",
        "payment_method": "bank_transfer",
        "notes": "První verze draftu",
        "items": [
            {
                "description": "Implementace API",
                "quantity": "1",
                "unit": "ks",
                "unit_price": "100.00",
                "vat_rate": "21",
            }
        ],
    }
    create_response = client.post("/api/v1/subjects/1/invoices", headers=owner_headers, json=create_payload)
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "draft"
    assert created["number"].startswith("DRAFT-")
    assert created["total"] == "121.00"
    assert created["variable_symbol"] == "20260001"
    assert created["invoice_language"] == "en"
    assert created["invoice_style"] == "modern"
    assert created["public_link"]["enabled"] is True
    assert created["public_link"]["isdoc_url"].endswith("/draft-1/isdoc")
    assert created["public_link"]["isdoc_download_url"].endswith("/draft-1/isdoc?download=1")
    invoice_id = created["id"]

    create_replay = client.post("/api/v1/subjects/1/invoices", headers=owner_headers, json=create_payload)
    assert create_replay.status_code == 201
    assert create_replay.json()["id"] == invoice_id

    patch_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "invoice-patch-1",
    }
    patch_payload = {
        "notes": "Aktualizovaný draft",
        "invoice_language": "cs",
        "invoice_style": "modern",
        "discount": "20.00",
        "items": [
            {
                "description": "Implementace API - rozšíření",
                "quantity": "1",
                "unit": "ks",
                "unit_price": "200.00",
                "vat_rate": "21",
            }
        ],
    }
    patch_response = client.patch(
        f"/api/v1/subjects/1/invoices/{invoice_id}",
        headers=patch_headers,
        json=patch_payload,
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["notes"] == "Aktualizovaný draft"
    assert patched["discount"] == "20.00"
    assert patched["invoice_language"] == "cs"
    assert patched["invoice_style"] == "modern"
    assert patched["total"] == "222.00"
    assert patched["items"][0]["description"] == "Implementace API - rozšíření"

    issue_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "invoice-issue-1",
    }
    issue_response = client.post(
        f"/api/v1/subjects/1/invoices/{invoice_id}/issue",
        headers=issue_headers,
    )
    assert issue_response.status_code == 200
    issued = issue_response.json()
    assert issued["status"] == "issued"
    assert issued["number"] == "2026-0001"
    assert issued["issued_at"] is not None
    assert issued["variable_symbol"] == "20260001"

    issue_replay = client.post(
        f"/api/v1/subjects/1/invoices/{invoice_id}/issue",
        headers=issue_headers,
    )
    assert issue_replay.status_code == 200
    assert issue_replay.json()["number"] == "2026-0001"

    patch_after_issue = client.patch(
        f"/api/v1/subjects/1/invoices/{invoice_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"notes": "tohle už nesmí projít"},
    )
    assert patch_after_issue.status_code == 409
    assert patch_after_issue.json()["error"]["code"] == "invoice_not_draft"

    editor_create_denied = client.post(
        "/api/v1/subjects/1/invoices",
        headers={"Authorization": f"Bearer {editor_token}"},
        json={
            "contact_id": 1,
            "issue_date": "2026-03-25",
            "due_date": "2026-04-08",
            "items": [
                {
                    "description": "Editor draft",
                    "quantity": "1",
                    "unit_price": "10.00",
                    "vat_rate": "21",
                }
            ],
        },
    )
    assert editor_create_denied.status_code == 403
    assert editor_create_denied.json()["error"]["code"] == "subject_access_denied"

    second_draft = client.post(
        "/api/v1/subjects/1/invoices",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "contact_id": 1,
            "issue_date": "2026-03-25",
            "due_date": "2026-04-08",
            "items": [
                {
                    "description": "Druhý draft",
                    "quantity": "1",
                    "unit_price": "10.00",
                    "vat_rate": "21",
                }
            ],
        },
    )
    assert second_draft.status_code == 201
    second_invoice_id = second_draft.json()["id"]

    forbidden_issue = client.post(
        f"/api/v1/subjects/1/invoices/{second_invoice_id}/issue",
        headers={"Authorization": f"Bearer {editor_token}"},
    )
    assert forbidden_issue.status_code == 403
    assert forbidden_issue.json()["error"]["code"] == "subject_access_denied"

    with SessionLocal() as db:
        from fakturek.models import Invoice

        invoices = db.query(Invoice).filter(Invoice.subject_id == 1).order_by(Invoice.id.asc()).all()
        assert len(invoices) == 2
        assert invoices[0].number == "2026-0001"
        assert invoices[0].status == "issued"
        assert invoices[0].invoice_language == "cs"
        assert invoices[0].invoice_style == "modern"
        assert invoices[1].status == "draft"
        assert invoices[1].number.startswith("DRAFT-")


def test_api_v1_sandbox_invoice_preview_does_not_persist(monkeypatch, tmp_path):
    client, SessionLocal, _owner_token, _editor_token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    with SessionLocal() as db:
        _row, sandbox_token = create_api_token(
            db,
            user_id=1,
            subject_id=1,
            name="Sandbox token",
            can_read=True,
            can_write=True,
            can_issue=True,
            can_export=False,
            is_sandbox=True,
        )
        db.commit()

    payload = {
        "contact_id": 1,
        "issue_date": "2026-03-24",
        "due_date": "2026-04-07",
        "items": [
            {
                "description": "Sandbox faktura",
                "quantity": "1",
                "unit": "ks",
                "unit_price": "100.00",
                "vat_rate": "21",
            }
        ],
    }
    headers = {"Authorization": f"Bearer {sandbox_token}"}
    preview_response = client.post("/api/v1/subjects/1/sandbox/invoices", headers=headers, json=payload)
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["sandbox"] is True
    assert preview["persisted"] is False
    assert preview["invoice"]["status"] == "issued"
    assert preview["invoice"]["number"] == "2026-0001"
    assert preview["invoice"]["total"] == "121.00"

    real_create_response = client.post("/api/v1/subjects/1/invoices", headers=headers, json=payload)
    assert real_create_response.status_code == 403
    assert real_create_response.json()["error"]["code"] == "api_token_sandbox_only"

    with SessionLocal() as db:
        from fakturek.models import Invoice, InvoiceSeries

        invoices = db.query(Invoice).filter(Invoice.subject_id == 1).all()
        assert invoices == []
        series = db.query(InvoiceSeries).filter(InvoiceSeries.subject_id == 1).all()
        assert series == []

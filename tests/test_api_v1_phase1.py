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


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_api_app(monkeypatch, tmp_path):
    db_path = tmp_path / "api-v1-phase1.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import (
        Contact,
        Invoice,
        InvoiceItem,
        InvoiceParty,
        Payment,
        Subject,
        User,
        UserSubject,
    )

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
        outsider = User(
            id=2,
            username="outsider",
            email="outsider@example.test",
            password_hash=hash_password("pw", iterations=1000),
            is_active=True,
        )
        db.add_all([owner, outsider])

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
        )
        denied_subject = Subject(
            id=2,
            name="Secret s.r.o.",
            email="secret@example.test",
            city="Praha",
            street="Tajná 1",
            zip="11000",
            country="CZ",
            default_currency="CZK",
            public_username="secret",
        )
        db.add_all([subject, denied_subject])

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
                subject_id=2,
                role="owner",
                can_view=True,
                can_edit=True,
                can_issue=True,
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

        invoice = Invoice(
            id=1,
            subject_id=1,
            contact_id=1,
            number="2026-0001",
            status="issued",
            document_type="invoice",
            issue_date=date(2026, 3, 10),
            due_date=date(2026, 3, 20),
            currency="CZK",
            variable_symbol="20260001",
            total_cents=25_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            public_token="public-token-1",
            pdf_path="var/pdfs/invoice-1.pdf",
        )
        db.add(invoice)
        db.add(
            InvoiceItem(
                id=1,
                invoice_id=1,
                description="Vývoj API vrstvy",
                quantity=Decimal("2.00"),
                unit="hod",
                unit_price_cents=12_500,
                vat_rate=Decimal("21.00"),
                line_net_cents=25_000,
                line_vat_cents=5_250,
                line_total_cents=30_250,
                sort_order=1,
            )
        )
        db.add_all(
            [
                InvoiceParty(
                    invoice_id=1,
                    role="buyer",
                    name="Acme Client a.s.",
                    email="ap@acme.test",
                    phone="",
                    street="Nádražní 1",
                    city="Praha",
                    zip="11000",
                    country="CZ",
                    ico="87654321",
                    dic="CZ87654321",
                ),
                InvoiceParty(
                    invoice_id=1,
                    role="seller",
                    name="Studio Alpha",
                    email="billing@studio-alpha.test",
                    phone="",
                    street="Křižíkova 12",
                    city="Brno",
                    zip="60200",
                    country="CZ",
                    ico="12345678",
                    dic="CZ12345678",
                ),
            ]
        )
        db.add(
            Payment(
                id=1,
                invoice_id=1,
                paid_on=date(2026, 3, 21),
                amount_cents=25_000,
                note="Ručně spárováno",
            )
        )

        _row, plain_token = create_api_token(db, user_id=1, name="Tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, plain_token


def test_api_v1_requires_bearer_token(monkeypatch, tmp_path):
    client, _SessionLocal, _token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    response = client.get("/api/v1/me")

    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "auth_missing_bearer"
    assert payload["error"]["message"]


def test_api_v1_me_and_subject_list_work_with_token(monkeypatch, tmp_path):
    client, _SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {token}"}

    me_response = client.get("/api/v1/me", headers=headers)
    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["user"]["username"] == "owner"
    assert me_payload["token"]["name"] == "Tests"
    assert len(me_payload["subjects"]) == 1
    assert me_payload["subjects"][0]["name"] == "Studio Alpha"

    list_response = client.get("/api/v1/subjects", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total_items"] == 1
    assert list_payload["items"][0]["permissions"]["can_issue"] is True


def test_api_v1_contact_and_invoice_detail_include_expected_fields(monkeypatch, tmp_path):
    client, _SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {token}"}

    contact_response = client.get("/api/v1/subjects/1/contacts/1", headers=headers)
    assert contact_response.status_code == 200
    contact_payload = contact_response.json()
    assert contact_payload["name"] == "Acme Client a.s."
    assert contact_payload["fixed_variable_symbol"] == "20260001"

    invoice_response = client.get("/api/v1/subjects/1/invoices/1", headers=headers)
    assert invoice_response.status_code == 200
    invoice_payload = invoice_response.json()
    assert invoice_payload["number"] == "2026-0001"
    assert invoice_payload["total"] == "250.00"
    assert invoice_payload["pdf_available"] is True
    assert invoice_payload["public_link"]["enabled"] is True
    assert invoice_payload["public_link"]["url"].startswith("https://billing.example.test/studio-alpha/i/")
    assert invoice_payload["public_link"]["short_url"].startswith("https://billing.example.test/i/")
    assert len(invoice_payload["items"]) == 1
    assert invoice_payload["items"][0]["quantity"] == "2.00"
    assert len(invoice_payload["payments"]) == 1
    assert invoice_payload["payments"][0]["amount"] == "250.00"


def test_api_v1_filters_and_denied_subject(monkeypatch, tmp_path):
    client, _SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {token}"}

    invoices_response = client.get(
        "/api/v1/subjects/1/invoices?status=issued&document_type=invoice&overdue=true&q=2026-0001",
        headers=headers,
    )
    assert invoices_response.status_code == 200
    invoices_payload = invoices_response.json()
    assert invoices_payload["total_items"] == 1
    assert invoices_payload["items"][0]["contact"]["name"] == "Acme Client a.s."

    empty_response = client.get("/api/v1/subjects/1/invoices?document_type=quote", headers=headers)
    assert empty_response.status_code == 200
    assert empty_response.json()["total_items"] == 0

    contacts_response = client.get("/api/v1/subjects/1/contacts?q=Acme", headers=headers)
    assert contacts_response.status_code == 200
    assert contacts_response.json()["total_items"] == 1

    denied_response = client.get("/api/v1/subjects/2", headers=headers)
    assert denied_response.status_code == 403
    assert denied_response.json()["error"]["code"] == "subject_access_denied"


def test_api_v1_openapi_yaml_and_health_are_public(monkeypatch, tmp_path):
    client, _SessionLocal, _token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    health_response = client.get("/api/v1/healthz")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"

    openapi_response = client.get("/api/v1/openapi.yaml")
    assert openapi_response.status_code == 200
    assert "Fakturek API v1" in openapi_response.text
    assert "/subjects/{subject_id}/invoices/{invoice_id}" in openapi_response.text

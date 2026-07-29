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
    db_path = tmp_path / "api-v1-phase5.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, InvoiceCatalogItem, InvoiceSeries, Subject, SubjectBankAccount, User, UserSubject

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
        viewer = User(
            id=2,
            username="viewer",
            email="viewer@example.test",
            password_hash=hash_password("pw", iterations=1000),
            is_active=True,
        )
        db.add_all([owner, viewer])

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
                role="viewer",
                can_view=True,
                can_edit=False,
                can_issue=False,
            )
        )

        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Acme Client a.s.",
                email="ap@acme.test",
                city="Praha",
                country="CZ",
                ico="87654321",
            )
        )

        db.add_all(
            [
                InvoiceSeries(
                    id=1,
                    subject_id=1,
                    name="default",
                    prefix="",
                    pad_length=4,
                    last_counter=12,
                    last_counter_year=2026,
                ),
                InvoiceSeries(
                    id=2,
                    subject_id=1,
                    name="quote",
                    prefix="NAB",
                    pad_length=4,
                    last_counter=3,
                    last_counter_year=2026,
                ),
            ]
        )

        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2026-0015",
                status="issued",
                document_type="invoice",
                issue_date=date(2026, 3, 1),
                due_date=date(2026, 3, 15),
                currency="CZK",
                variable_symbol="20260015",
                total_cents=12_100,
                discount_cents=0,
                rounding_adjustment_cents=0,
                payment_method="bank_transfer",
                buyer_name_cache="Acme Client a.s.",
                series_id=1,
            )
        )

        db.add_all(
            [
                SubjectBankAccount(
                    id=1,
                    subject_id=1,
                    label="Hlavní účet",
                    account_number="2200041594/2010",
                    iban="CZ0420100000002200041594",
                    bic="FIOBCZPP",
                    country="CZ",
                    currency="CZK",
                    is_default=True,
                    sort_order=1,
                    payment_sync_provider="fio_api",
                    payment_sync_enabled=True,
                    payment_sync_auto_pair=True,
                ),
                SubjectBankAccount(
                    id=2,
                    subject_id=1,
                    label="EUR účet",
                    account_number="",
                    iban="SK3112000000198742637541",
                    bic="TATRSKBX",
                    country="SK",
                    currency="EUR",
                    is_default=False,
                    sort_order=2,
                    payment_sync_provider="none",
                    payment_sync_enabled=False,
                    payment_sync_auto_pair=True,
                ),
            ]
        )

        db.add_all(
            [
                InvoiceCatalogItem(
                    id=1,
                    subject_id=1,
                    description="Audit API rozhraní",
                    quantity=Decimal("2.00"),
                    unit="hod",
                    unit_price_cents=15000,
                    vat_rate=Decimal("21.00"),
                    currency="CZK",
                ),
                InvoiceCatalogItem(
                    id=2,
                    subject_id=1,
                    description="Monitoring SLA",
                    quantity=Decimal("1.00"),
                    unit="měs",
                    unit_price_cents=9900,
                    vat_rate=Decimal("21.00"),
                    currency="EUR",
                ),
            ]
        )

        _row_owner, owner_token = create_api_token(db, user_id=1, name="Owner tests")
        _row_viewer, viewer_token = create_api_token(db, user_id=2, name="Viewer tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, owner_token, viewer_token


def test_api_v1_lists_series_and_bank_accounts(monkeypatch, tmp_path):
    client, _SessionLocal, owner_token, _viewer_token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {owner_token}"}

    series_response = client.get("/api/v1/subjects/1/invoice-series?year=2026", headers=headers)
    assert series_response.status_code == 200
    series_payload = series_response.json()
    assert series_payload["total_items"] == 2
    assert series_payload["items"][0]["name"] == "default"
    assert series_payload["items"][0]["next_number_preview"] == "2026-0016"

    quote_only = client.get("/api/v1/subjects/1/invoice-series?document_type=quote&year=2026", headers=headers)
    assert quote_only.status_code == 200
    assert quote_only.json()["items"][0]["next_number_preview"] == "2026-NAB-0004"

    series_detail = client.get("/api/v1/subjects/1/invoice-series/1?year=2026", headers=headers)
    assert series_detail.status_code == 200
    assert series_detail.json()["next_number_preview"] == "2026-0016"

    bank_accounts = client.get("/api/v1/subjects/1/bank-accounts", headers=headers)
    assert bank_accounts.status_code == 200
    bank_payload = bank_accounts.json()
    assert bank_payload["total_items"] == 2
    assert bank_payload["items"][0]["label"] == "Hlavní účet"
    assert bank_payload["items"][0]["payment_sync_provider"] == "fio_api"
    assert bank_payload["items"][1]["currency"] == "EUR"
    assert bank_payload["items"][1]["display_account"].startswith("SK31")

    eur_accounts = client.get("/api/v1/subjects/1/bank-accounts?currency=eur", headers=headers)
    assert eur_accounts.status_code == 200
    assert eur_accounts.json()["total_items"] == 1
    assert eur_accounts.json()["items"][0]["label"] == "EUR účet"

    account_detail = client.get("/api/v1/subjects/1/bank-accounts/1", headers=headers)
    assert account_detail.status_code == 200
    detail_payload = account_detail.json()
    assert detail_payload["account_number"] == "2200041594/2010"
    assert detail_payload["iban_display"] == "CZ04 2010 0000 0022 0004 1594"
    assert "fio_api_token" not in detail_payload



def test_api_v1_catalog_item_crud_and_idempotency(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, viewer_token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "catalog-create-1",
    }
    payload = {
        "description": "API maintenance",
        "quantity": "3",
        "unit": "hod",
        "unit_price": "175.50",
        "vat_rate": "21",
        "currency": "eur",
    }
    create_response = client.post("/api/v1/subjects/1/catalog-items", headers=headers, json=payload)
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["description"] == "API maintenance"
    assert created["quantity"] == "3.00"
    assert created["unit_price"] == "175.50"
    assert created["vat_rate"] == "21.00"
    assert created["currency"] == "EUR"
    created_id = created["id"]

    replay = client.post("/api/v1/subjects/1/catalog-items", headers=headers, json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == created_id

    mismatch = client.post(
        "/api/v1/subjects/1/catalog-items",
        headers=headers,
        json={**payload, "description": "Changed"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_key_reused"

    patch_response = client.patch(
        f"/api/v1/subjects/1/catalog-items/{created_id}",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "catalog-patch-1"},
        json={"description": "API maintenance premium", "unit_price": "199.00"},
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["description"] == "API maintenance premium"
    assert patched["unit_price"] == "199.00"

    list_response = client.get("/api/v1/subjects/1/catalog-items?q=maintenance&currency=EUR", headers={"Authorization": f"Bearer {owner_token}"})
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total_items"] == 1
    assert list_payload["items"][0]["id"] == created_id

    detail_response = client.get(
        f"/api/v1/subjects/1/catalog-items/{created_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["description"] == "API maintenance premium"

    forbidden = client.post(
        "/api/v1/subjects/1/catalog-items",
        headers={"Authorization": f"Bearer {viewer_token}"},
        json={"description": "Should fail"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "subject_access_denied"

    delete_headers = {"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "catalog-delete-1"}
    delete_response = client.delete(f"/api/v1/subjects/1/catalog-items/{created_id}", headers=delete_headers)
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True, "id": created_id}

    delete_replay = client.delete(f"/api/v1/subjects/1/catalog-items/{created_id}", headers=delete_headers)
    assert delete_replay.status_code == 200
    assert delete_replay.json() == {"deleted": True, "id": created_id}

    with SessionLocal() as db:
        from fakturek.models import InvoiceCatalogItem

        row = db.get(InvoiceCatalogItem, created_id)
        assert row is None



def test_api_v1_catalog_item_validation_returns_api_error(monkeypatch, tmp_path):
    client, _SessionLocal, owner_token, _viewer_token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/subjects/1/catalog-items",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"description": "", "quantity": "0", "unit_price": "-10"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] in {
        "catalog_item_description_required",
        "catalog_item_quantity_invalid",
        "catalog_item_unit_price_invalid",
    }

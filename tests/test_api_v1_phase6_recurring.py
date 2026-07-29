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
    db_path = tmp_path / "api-v1-phase6.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, InvoiceItem, InvoiceSeries, Subject, User, UserSubject

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

        db.add_all(
            [
                UserSubject(
                    user_id=1,
                    subject_id=1,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                UserSubject(
                    user_id=2,
                    subject_id=1,
                    role="editor",
                    can_view=True,
                    can_edit=True,
                    can_issue=False,
                ),
            ]
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

        series = InvoiceSeries(
            id=1,
            subject_id=1,
            name="default",
            prefix="2026-",
            pad_length=4,
            last_counter=1,
            last_counter_year=2026,
        )
        db.add(series)

        template_invoice = Invoice(
            id=1,
            subject_id=1,
            contact_id=1,
            number="TPL-2026-0001",
            status="issued",
            document_type="invoice",
            issue_date=date(2026, 3, 10),
            due_date=date(2026, 3, 20),
            currency="CZK",
            variable_symbol="20260001",
            total_cents=121_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            notes="Služby za {{period_label+1}}",
            footer_mode="trade_register",
            footer_text="Období {{year}}/{{month++}}",
            series_id=1,
        )
        credit_note_template = Invoice(
            id=2,
            subject_id=1,
            contact_id=1,
            number="CN-2026-0001",
            status="issued",
            document_type="credit_note",
            issue_date=date(2026, 3, 5),
            due_date=date(2026, 3, 5),
            currency="CZK",
            variable_symbol="20260002",
            total_cents=-12_100,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            series_id=1,
            source_invoice_id=1,
        )
        db.add_all([template_invoice, credit_note_template])
        db.add(
            InvoiceItem(
                id=1,
                invoice_id=1,
                description="Správa API za {{period_label+1}} ({{month_start+1}} až {{month_end+1}})",
                quantity=Decimal("1.00"),
                unit="měs",
                unit_price_cents=100_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=100_000,
                line_vat_cents=21_000,
                line_total_cents=121_000,
                sort_order=1,
            )
        )
        db.add(
            InvoiceItem(
                id=2,
                invoice_id=2,
                description="Dobropis test",
                quantity=Decimal("1.00"),
                unit="ks",
                unit_price_cents=-10_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=-10_000,
                line_vat_cents=-2_100,
                line_total_cents=-12_100,
                sort_order=1,
            )
        )

        _row_owner, owner_token = create_api_token(db, user_id=1, name="Owner tests")
        _row_editor, editor_token = create_api_token(db, user_id=2, name="Editor tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, owner_token, editor_token


def test_api_v1_recurring_plans_crud_run_and_idempotency(monkeypatch, tmp_path):
    client, SessionLocal, owner_token, _editor_token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    create_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "recurring-create-1",
    }
    create_payload = {
        "template_invoice_id": 1,
        "name": "Měsíční API support",
        "interval_unit": "month",
        "interval_count": 1,
        "next_issue_date": date.today().isoformat(),
        "due_in_days": 10,
        "auto_issue": True,
        "auto_send": False,
    }

    create_response = client.post(
        "/api/v1/subjects/1/recurring-plans",
        headers=create_headers,
        json=create_payload,
    )
    assert create_response.status_code == 201
    created_plan = create_response.json()
    assert created_plan["name"] == "Měsíční API support"
    assert created_plan["template_invoice"]["id"] == 1
    assert created_plan["interval_unit"] == "month"
    assert created_plan["auto_issue"] is True
    plan_id = created_plan["id"]

    create_replay = client.post(
        "/api/v1/subjects/1/recurring-plans",
        headers=create_headers,
        json=create_payload,
    )
    assert create_replay.status_code == 201
    assert create_replay.json()["id"] == plan_id

    list_response = client.get(
        "/api/v1/subjects/1/recurring-plans?active=true",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total_items"] == 1
    assert list_payload["items"][0]["id"] == plan_id

    detail_response = client.get(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["template_invoice"]["number"] == "TPL-2026-0001"

    patch_response = client.patch(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": "recurring-patch-1",
        },
        json={
            "email_override": "billing+override@example.test",
            "due_in_days": 7,
        },
    )
    assert patch_response.status_code == 200
    patched_plan = patch_response.json()
    assert patched_plan["email_override"] == "billing+override@example.test"
    assert patched_plan["due_in_days"] == 7

    run_headers = {
        "Authorization": f"Bearer {owner_token}",
        "Idempotency-Key": "recurring-run-1",
    }
    run_response = client.post(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}/run",
        headers=run_headers,
        json={},
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["created"] is True
    assert run_payload["emailed"] is False
    assert run_payload["errors"] == []
    created_invoice = run_payload["created_invoice"]
    assert created_invoice is not None
    assert created_invoice["status"] == "issued"
    assert created_invoice["source_invoice_id"] == 1
    assert "{{" not in created_invoice["items"][0]["description"]
    assert "Správa API za" in created_invoice["items"][0]["description"]
    assert " až " in created_invoice["items"][0]["description"]
    created_invoice_id = created_invoice["id"]

    run_replay = client.post(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}/run",
        headers=run_headers,
        json={},
    )
    assert run_replay.status_code == 200
    assert run_replay.json()["created_invoice"]["id"] == created_invoice_id

    with SessionLocal() as db:
        from fakturek.models import Invoice, RecurringInvoicePlan

        invoices = db.query(Invoice).filter(Invoice.subject_id == 1).order_by(Invoice.id.asc()).all()
        assert len(invoices) == 3
        plan = db.get(RecurringInvoicePlan, plan_id)
        assert plan is not None
        assert int(plan.last_generated_invoice_id or 0) == created_invoice_id
        assert plan.next_issue_date > date.today()

    delete_response = client.delete(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": "recurring-delete-1",
        },
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True, "id": plan_id}

    missing_response = client.get(
        f"/api/v1/subjects/1/recurring-plans/{plan_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "recurring_plan_not_found"

    openapi_response = client.get(
        "/api/v1/openapi.json",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert openapi_response.status_code == 200
    openapi_body = openapi_response.json()
    assert openapi_body["info"]["version"] == "1.0.0-phase8"
    assert "/subjects/{subject_id}/recurring-plans" in openapi_body["paths"]
    assert "/subjects/{subject_id}/recurring-plans/{plan_id}/run" in openapi_body["paths"]


def test_api_v1_recurring_plans_permissions_and_validation(monkeypatch, tmp_path):
    client, _SessionLocal, owner_token, editor_token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    denied_response = client.post(
        "/api/v1/subjects/1/recurring-plans",
        headers={"Authorization": f"Bearer {editor_token}"},
        json={
            "template_invoice_id": 1,
            "name": "Editor plan",
        },
    )
    assert denied_response.status_code == 403
    assert denied_response.json()["error"]["code"] == "subject_access_denied"

    invalid_template_response = client.post(
        "/api/v1/subjects/1/recurring-plans",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "template_invoice_id": 2,
            "name": "Credit note plan",
        },
    )
    assert invalid_template_response.status_code == 422
    assert invalid_template_response.json()["error"]["code"] == "recurring_template_credit_note_invalid"

    invalid_auto_send_response = client.post(
        "/api/v1/subjects/1/recurring-plans",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "template_invoice_id": 1,
            "auto_issue": False,
            "auto_send": True,
        },
    )
    assert invalid_auto_send_response.status_code == 422
    assert invalid_auto_send_response.json()["error"]["code"] == "recurring_auto_send_requires_auto_issue"

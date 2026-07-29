from __future__ import annotations

from datetime import date, datetime
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
    db_path = tmp_path / "api-v1-phase7.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import (
        BankTransaction,
        Contact,
        Invoice,
        InvoiceItem,
        Subject,
        SubjectBankAccount,
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
        db.add(owner)

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

        contact = Contact(
            id=1,
            subject_id=1,
            name="Acme Client a.s.",
            email="ap@acme.test",
            city="Praha",
            country="CZ",
            ico="87654321",
            fixed_variable_symbol="20260009",
        )
        db.add(contact)

        db.add(
            SubjectBankAccount(
                id=1,
                subject_id=1,
                label="Fio účet",
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
            )
        )

        draft_invoice = Invoice(
            id=1,
            subject_id=1,
            contact_id=1,
            number="DRAFT-1",
            status="draft",
            document_type="invoice",
            issue_date=date(2026, 3, 10),
            due_date=date(2026, 3, 20),
            currency="CZK",
            total_cents=10_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            bank_account_id=1,
        )
        issued_invoice = Invoice(
            id=2,
            subject_id=1,
            contact_id=1,
            number="2026-0012",
            status="issued",
            document_type="invoice",
            issue_date=date(2026, 3, 11),
            due_date=date(2026, 3, 21),
            currency="CZK",
            total_cents=12_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            bank_account_id=1,
            variable_symbol="20260012",
        )
        sent_invoice = Invoice(
            id=3,
            subject_id=1,
            contact_id=1,
            number="2026-0009",
            status="sent",
            document_type="invoice",
            issue_date=date(2026, 3, 13),
            due_date=date(2026, 4, 6),
            currency="CZK",
            total_cents=700_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            payment_method="bank_transfer",
            buyer_name_cache="Acme Client a.s.",
            bank_account_id=1,
            variable_symbol="20260009",
            sent_at=datetime(2026, 3, 13, 10, 0, 0),
        )
        db.add_all([draft_invoice, issued_invoice, sent_invoice])
        db.add_all(
            [
                InvoiceItem(
                    id=1,
                    invoice_id=1,
                    description="Draft práce",
                    quantity=Decimal("1.00"),
                    unit="hod",
                    unit_price_cents=10_000,
                    vat_rate=Decimal("0.00"),
                    line_net_cents=10_000,
                    line_vat_cents=0,
                    line_total_cents=10_000,
                    sort_order=1,
                ),
                InvoiceItem(
                    id=2,
                    invoice_id=2,
                    description="Hotová práce",
                    quantity=Decimal("1.00"),
                    unit="hod",
                    unit_price_cents=12_000,
                    vat_rate=Decimal("0.00"),
                    line_net_cents=12_000,
                    line_vat_cents=0,
                    line_total_cents=12_000,
                    sort_order=1,
                ),
                InvoiceItem(
                    id=3,
                    invoice_id=3,
                    description="Roční servis",
                    quantity=Decimal("1.00"),
                    unit="rok",
                    unit_price_cents=700_000,
                    vat_rate=Decimal("0.00"),
                    line_net_cents=700_000,
                    line_vat_cents=0,
                    line_total_cents=700_000,
                    sort_order=1,
                ),
            ]
        )

        db.add(
            BankTransaction(
                id=1,
                subject_bank_account_id=1,
                provider="fio_api",
                external_id="fio-2026-0009",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Faktura 2026-0009 | Acme Client a.s.",
                raw_payload_json='{"id":"fio-2026-0009","secret":"do-not-expose"}',
            )
        )

        _row, plain_token = create_api_token(db, user_id=1, name="Owner tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, plain_token


def test_api_v1_phase7_bulk_workflow_actions(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "bulk-paid-1"}

    paid_response = client.post(
        "/api/v1/subjects/1/invoices/bulk-action",
        headers=headers,
        json={
            "action": "paid",
            "invoice_ids": [2, 3, 1],
            "paid_on": "2026-03-20",
        },
    )
    assert paid_response.status_code == 200
    payload = paid_response.json()
    assert payload["action"] == "paid"
    assert payload["changed_count"] == 2
    assert payload["skipped_count"] == 1
    assert payload["deleted_count"] == 0
    assert payload["items"][0]["invoice_id"] == 2
    assert payload["items"][2]["message"]

    replay = client.post(
        "/api/v1/subjects/1/invoices/bulk-action",
        headers=headers,
        json={
            "action": "paid",
            "invoice_ids": [2, 3, 1],
            "paid_on": "2026-03-20",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["changed_count"] == 2

    issue_response = client.post(
        "/api/v1/subjects/1/invoices/bulk-action",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bulk-issue-1"},
        json={
            "action": "issue",
            "invoice_ids": [1],
        },
    )
    assert issue_response.status_code == 200
    issue_payload = issue_response.json()
    assert issue_payload["changed_count"] == 1
    assert issue_payload["items"][0]["to_status"] == "issued"

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice_1 = db.get(Invoice, 1)
        invoice_2 = db.get(Invoice, 2)
        invoice_3 = db.get(Invoice, 3)
        assert invoice_1 is not None and invoice_1.status == "issued" and not str(invoice_1.number).startswith("DRAFT-")
        assert invoice_2 is not None and invoice_2.status == "paid" and invoice_2.paid_on == date(2026, 3, 20)
        assert invoice_3 is not None and invoice_3.status == "paid" and invoice_3.paid_on == date(2026, 3, 20)

    _reset_settings_and_db()


def test_api_v1_phase7_manual_payment_crud(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    create_response = client.post(
        "/api/v1/subjects/1/invoices/2/payments",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "payment-create-1"},
        json={
            "paid_on": "2026-03-22",
            "amount": "120.00",
            "note": "Ručně doplněno",
        },
    )
    assert create_response.status_code == 201
    payment_payload = create_response.json()
    payment_id = payment_payload["id"]
    assert payment_payload["amount"] == "120.00"
    assert payment_payload["bank_transaction_ids"] == []

    patch_response = client.patch(
        f"/api/v1/subjects/1/invoices/2/payments/{payment_id}",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "payment-patch-1"},
        json={"note": "Opravená poznámka", "amount": "130.00"},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["amount"] == "130.00"
    assert patch_response.json()["note"] == "Opravená poznámka"

    delete_response = client.delete(
        f"/api/v1/subjects/1/invoices/2/payments/{payment_id}",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "payment-delete-1"},
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True, "id": payment_id}

    from fakturek.models import Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 2)
        assert invoice is not None
        assert invoice.status == "issued"
        assert invoice.paid_on is None
        assert db.get(Payment, payment_id) is None

    _reset_settings_and_db()


def test_api_v1_phase7_bank_transaction_match_unmatch_and_retry(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    auth = {"Authorization": f"Bearer {token}"}

    match_response = client.post(
        "/api/v1/subjects/1/bank-transactions/1/match",
        headers={**auth, "Idempotency-Key": "tx-match-1"},
        json={"invoice_id": 3},
    )
    assert match_response.status_code == 200
    match_payload = match_response.json()
    assert match_payload["invoice_status"] == "paid"
    assert match_payload["payment"]["bank_transaction_ids"] == [1]
    assert match_payload["transaction"]["matched_invoice_id"] == 3

    detail_response = client.get("/api/v1/subjects/1/bank-transactions/1", headers=auth)
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["matched_invoice_id"] == 3
    assert "raw_payload_json" not in detail_payload
    assert detail_payload["counterparty_name"] == "Acme Client a.s."

    unmatch_response = client.post(
        "/api/v1/subjects/1/bank-transactions/1/unmatch",
        headers={**auth, "Idempotency-Key": "tx-unmatch-1"},
    )
    assert unmatch_response.status_code == 200
    unmatch_payload = unmatch_response.json()
    assert unmatch_payload["transaction"]["matched_invoice_id"] is None
    assert unmatch_payload["deleted_payment_id"] is not None
    assert unmatch_payload["invoice_status"] == "sent"

    retry_response = client.post(
        "/api/v1/subjects/1/bank-accounts/1/retry-matching",
        headers={**auth, "Idempotency-Key": "tx-retry-1"},
    )
    assert retry_response.status_code == 200
    retry_payload = retry_response.json()
    assert retry_payload["bank_account_id"] == 1
    assert retry_payload["matched"] == 1
    assert retry_payload["remaining_unmatched"] == 0

    tx_list_response = client.get(
        "/api/v1/subjects/1/bank-transactions?matched=true",
        headers=auth,
    )
    assert tx_list_response.status_code == 200
    assert tx_list_response.json()["total_items"] == 1

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 3)
        tx = db.get(BankTransaction, 1)
        payments = db.scalars(sqlalchemy.select(Payment).where(Payment.invoice_id == 3)).all()
        assert invoice is not None and invoice.status == "paid" and invoice.paid_on == date(2026, 3, 17)
        assert tx is not None and tx.matched_invoice_id == 3 and tx.payment_id is not None
        assert len(payments) == 1

    _reset_settings_and_db()


def test_api_v1_phase7_openapi_contains_bulk_payments_and_bank_matching(monkeypatch, tmp_path):
    client, _SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    response = client.get(
        "/api/v1/openapi.json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["info"]["version"] == "1.0.0-phase8"
    assert "/subjects/{subject_id}/invoices/bulk-action" in body["paths"]
    assert "/subjects/{subject_id}/invoices/{invoice_id}/payments" in body["paths"]
    assert "/subjects/{subject_id}/bank-transactions/{transaction_id}/match" in body["paths"]
    assert "/subjects/{subject_id}/bank-accounts/{bank_account_id}/retry-matching" in body["paths"]

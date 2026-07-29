from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

import fakturek.db as db_module
from fakturek.api_tokens import create_api_token
from fakturek.auth import hash_password
from fakturek.bank_sync import ImportedBankEmail, ImportedBankTransaction
from fakturek.db import Base
from fakturek.settings import get_settings

sqlalchemy = pytest.importorskip("sqlalchemy")


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_api_app(monkeypatch, tmp_path):
    db_path = tmp_path / "api-v1-phase8.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billing.example.test")
    monkeypatch.setenv("FIO_API_BASE_URL", "https://fio.example.test")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_PORT", "993")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_USERNAME", "payments@example.test")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_PASSWORD", "secret")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_MAILBOX", "INBOX")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_USE_SSL", "1")
    monkeypatch.setenv("PAYMENT_SYNC_ALERT_DOMAIN", "alerts.example.test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, InvoiceItem, Subject, SubjectBankAccount, User, UserSubject

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

        db.add_all(
            [
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
                    fio_api_token="test-fio-token",
                ),
                SubjectBankAccount(
                    id=2,
                    subject_id=1,
                    label="Mail účet",
                    account_number="2400000001/2010",
                    iban="CZ3020100000002400000001",
                    bic="FIOBCZPP",
                    country="CZ",
                    currency="CZK",
                    is_default=False,
                    sort_order=2,
                    payment_sync_provider="email_bank",
                    payment_sync_enabled=True,
                    payment_sync_auto_pair=True,
                    payment_sync_email_parser="pending",
                    payment_sync_alert_localpart="alerts-acct-2",
                ),
            ]
        )

        db.add_all(
            [
                Invoice(
                    id=1,
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
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0010",
                    status="sent",
                    document_type="invoice",
                    issue_date=date(2026, 3, 14),
                    due_date=date(2026, 4, 7),
                    currency="CZK",
                    total_cents=500_000,
                    discount_cents=0,
                    rounding_adjustment_cents=0,
                    payment_method="bank_transfer",
                    buyer_name_cache="Acme Client a.s.",
                    bank_account_id=2,
                    variable_symbol="20260010",
                ),
            ]
        )
        db.add_all(
            [
                InvoiceItem(
                    id=1,
                    invoice_id=1,
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
                InvoiceItem(
                    id=2,
                    invoice_id=2,
                    description="Měsíční servis",
                    quantity=Decimal("1.00"),
                    unit="měs",
                    unit_price_cents=500_000,
                    vat_rate=Decimal("0.00"),
                    line_net_cents=500_000,
                    line_vat_cents=0,
                    line_total_cents=500_000,
                    sort_order=1,
                ),
            ]
        )

        _row, plain_token = create_api_token(db, user_id=1, name="Owner tests")
        db.commit()

    client = TestClient(create_app())
    return client, SessionLocal, plain_token


def test_api_v1_phase8_bank_account_sync_matches_fio_transaction(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankTransaction(
                provider="fio_api",
                external_id="fio-2026-0009",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol="20260009",
                constant_symbol=None,
                specific_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Úhrada faktury 2026-0009",
                raw_payload={"id": "fio-2026-0009"},
            )
        ]

    monkeypatch.setattr("fakturek.api_v1.fetch_fio_transactions", _fake_fetch)

    response = client.post(
        "/api/v1/subjects/1/bank-accounts/1/sync",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bank-sync-acct-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bank_account_id"] == 1
    assert body["imported"] == 1
    assert body["matched"] == 1
    assert body["errors"] == []

    replay = client.post(
        "/api/v1/subjects/1/bank-accounts/1/sync",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bank-sync-acct-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["matched"] == 1

    from fakturek.models import BankTransaction, Invoice, Payment, SubjectBankAccount

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        account = db.get(SubjectBankAccount, 1)
        transactions = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()
        assert invoice is not None and invoice.status == "paid" and invoice.paid_on == date(2026, 3, 17)
        assert account is not None and account.payment_sync_last_success_at is not None and account.payment_sync_last_error is None
        assert len(transactions) == 1 and transactions[0].matched_invoice_id == 1
        assert len(payments) == 1 and payments[0].invoice_id == 1

    _reset_settings_and_db()


def test_api_v1_bank_sync_does_not_match_payment_older_than_invoice(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "fakturek.api_v1.fetch_fio_transactions",
        lambda *args, **kwargs: [
            ImportedBankTransaction(
                provider="fio_api",
                external_id="older-payment",
                booked_on=date(2026, 3, 12),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol="20260009",
                constant_symbol=None,
                specific_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Úhrada faktury 2026-0009",
                raw_payload={"id": "older-payment"},
            )
        ],
    )

    response = client.post(
        "/api/v1/subjects/1/bank-accounts/1/sync",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "older-payment-sync"},
    )
    assert response.status_code == 200
    assert response.json()["matched"] == 0
    retry = client.post(
        "/api/v1/subjects/1/bank-accounts/1/sync",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "older-payment-sync-retry"},
    )
    assert retry.status_code == 200
    assert retry.json()["matched"] == 0

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        transaction = db.scalar(sqlalchemy.select(BankTransaction))
        assert invoice is not None and invoice.status == "sent" and invoice.paid_on is None
        assert transaction is not None and transaction.matched_invoice_id is None and transaction.payment_id is None
        assert db.scalar(sqlalchemy.select(sqlalchemy.func.count(Payment.id))) == 0

    _reset_settings_and_db()


def test_api_v1_phase8_import_transactions_supports_partial_results(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/subjects/1/bank-accounts/1/import-transactions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bank-import-1"},
        json={
            "items": [
                {
                    "external_id": "api-001",
                    "booked_on": "2026-03-18",
                    "amount": "7000.00",
                    "currency": "CZK",
                    "direction": "incoming",
                    "variable_symbol": "20260009",
                    "message": "API import 2026-0009",
                },
                {
                    "external_id": "api-002",
                    "booked_on": "2026-03-18",
                    "amount": "10.00",
                    "currency": "CZK",
                    "direction": "sideways",
                },
            ],
            "auto_pair": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested_count"] == 2
    assert body["imported_count"] == 1
    assert body["matched_count"] == 1
    assert body["skipped_existing_count"] == 0
    assert body["items"][0]["transaction"]["provider"] == "api_manual"
    assert body["items"][1]["result"] == "error"
    assert "direction" in body["items"][1]["message"]
    assert "raw_payload_json" not in body["items"][0]["transaction"]

    from fakturek.models import BankTransaction, Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        transactions = db.scalars(sqlalchemy.select(BankTransaction).order_by(BankTransaction.id.asc())).all()
        assert invoice is not None and invoice.status == "paid"
        assert len(transactions) == 1
        assert transactions[0].provider == "api_manual"
        assert transactions[0].matched_invoice_id == 1

    _reset_settings_and_db()


def test_api_v1_phase8_import_email_then_reprocess_with_parser(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)
    auth = {"Authorization": f"Bearer {token}"}

    import_response = client.post(
        "/api/v1/subjects/1/bank-accounts/2/import-email",
        headers={**auth, "Idempotency-Key": "bank-email-import-1"},
        json={
            "external_message_id": "<rb-1@example.test>",
            "received_at": "2026-03-25T20:57:00",
            "from_email": "info@rb.cz",
            "subject": "Pohyb na účtě",
            "body_text": (
                "Pohyb na účtě Datum a čas 25. 03. 2026 20:56 "
                "Na účet 2400000001/2010 Studio Alpha "
                "Částka v měně účtu +5 000,00 CZK "
                "Kategorie pohybu Platba "
                "Typ pohybu Příchozí úhrada "
                "Z účtu 5508932004/5500 Acme Client a.s. "
                "Variabilní symbol 20260010 "
                "Konstantní symbol 0308 "
                "Specifický symbol 0000 "
                "Zpráva pro příjemce Úhrada faktury 2026-0010"
            ),
        },
    )
    assert import_response.status_code == 200
    import_body = import_response.json()
    email_id = import_body["email"]["id"]
    assert import_body["matched"] is False
    assert import_body["transaction"] is None
    assert import_body["email"]["processing_status"] == "stored"
    assert import_body["email"]["body_preview"]

    list_response = client.get(
        "/api/v1/subjects/1/bank-incoming-emails?bank_account_id=2",
        headers=auth,
    )
    assert list_response.status_code == 200
    assert list_response.json()["total_items"] == 1
    assert "raw_headers_json" not in list_response.text

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 2)
        assert account is not None
        account.payment_sync_email_parser = "raiffeisenbank_cz"
        db.add(account)
        db.commit()

    reprocess_response = client.post(
        f"/api/v1/subjects/1/bank-incoming-emails/{email_id}/reprocess",
        headers={**auth, "Idempotency-Key": "bank-email-reprocess-1"},
        json={},
    )
    assert reprocess_response.status_code == 200
    reprocess_body = reprocess_response.json()
    assert reprocess_body["matched"] is True
    assert reprocess_body["transaction"]["provider"] == "email_bank_raiffeisenbank_cz"
    assert reprocess_body["email"]["processing_status"] == "matched"

    detail_response = client.get(
        f"/api/v1/subjects/1/bank-incoming-emails/{email_id}",
        headers=auth,
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["processing_status"] == "matched"

    from fakturek.models import BankIncomingEmail, BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 2)
        email_row = db.get(BankIncomingEmail, email_id)
        transactions = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()
        assert invoice is not None and invoice.status == "paid" and invoice.paid_on == date(2026, 3, 25)
        assert email_row is not None and email_row.processing_status == "matched" and email_row.matched_bank_transaction_id is not None
        assert len(transactions) == 1 and transactions[0].provider == "email_bank_raiffeisenbank_cz"
        assert len(payments) == 1 and payments[0].invoice_id == 2

    _reset_settings_and_db()


def test_api_v1_phase8_subject_sync_baseline_seeds_email_account(monkeypatch, tmp_path):
    client, SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    monkeypatch.setattr("fakturek.api_v1.fetch_fio_transactions", lambda *args, **kwargs: [])

    def _fake_fetch_emails(*args, **kwargs):
        return [
            ImportedBankEmail(
                provider="email_bank",
                imap_uid="812",
                external_message_id="<email-812@example.test>",
                received_at=datetime(2026, 3, 25, 21, 15, 0),
                from_email="info@rb.cz",
                subject="Pohyb na účtě",
                body_text="placeholder",
                raw_headers={
                    "From": "Raiffeisenbank <info@rb.cz>",
                    "To": "alerts-acct-2@alerts.example.test",
                    "Subject": "Pohyb na účtě",
                },
            )
        ]

    monkeypatch.setattr("fakturek.api_v1.fetch_imap_bank_emails", _fake_fetch_emails)

    response = client.post(
        "/api/v1/subjects/1/bank-sync/run",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bank-sync-subject-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subject_id"] == 1
    assert len(body["accounts"]) == 2
    assert body["baseline_seeded"] is True

    from fakturek.models import BankIncomingEmail, SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 2)
        emails = db.scalars(sqlalchemy.select(BankIncomingEmail)).all()
        assert account is not None and account.payment_sync_last_email_uid == "812"
        assert emails == []

    _reset_settings_and_db()


def test_api_v1_phase8_openapi_contains_bank_sync_and_email_endpoints(monkeypatch, tmp_path):
    client, _SessionLocal, token = _setup_sqlite_api_app(monkeypatch, tmp_path)

    response = client.get(
        "/api/v1/openapi.json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["info"]["version"] == "1.0.0-phase8"
    assert "/subjects/{subject_id}/bank-sync/run" in body["paths"]
    assert "/subjects/{subject_id}/bank-accounts/{bank_account_id}/sync" in body["paths"]
    assert "/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-transactions" in body["paths"]
    assert "/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-email" in body["paths"]
    assert "/subjects/{subject_id}/bank-incoming-emails" in body["paths"]
    assert "/subjects/{subject_id}/bank-incoming-emails/{email_id}/reprocess" in body["paths"]

    _reset_settings_and_db()

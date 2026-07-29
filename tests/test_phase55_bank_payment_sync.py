from __future__ import annotations

from datetime import date
from urllib.parse import unquote

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from datetime import datetime

from fakturek.bank_sync import (
    BankSyncError,
    ImportedBankEmail,
    ImportedBankTransaction,
    parse_csas_cz_email,
    parse_csob_cz_email,
    parse_fio_email_cz,
    parse_raiffeisenbank_cz_email,
)
from fakturek.db import Base
from fakturek.security import decrypt_secret
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "phase55.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_PORT", "993")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_USERNAME", "payments@example.test")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_PASSWORD", "secret")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_MAILBOX", "INBOX")
    monkeypatch.setenv("PAYMENT_SYNC_IMAP_USE_SSL", "1")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject, SubjectBankAccount

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Test subject",
                email="owner@example.test",
                city="Praha",
                country="CZ",
                default_currency="CZK",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Acme Client a.s.",
                email="billing@example.test",
                city="Praha",
                country="CZ",
                ico="12345678",
            )
        )
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
                fio_api_token="test-token",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2026-0009",
                status="sent",
                issue_date=date(2026, 3, 13),
                due_date=date(2026, 4, 6),
                currency="CZK",
                total_cents=700_000,
                variable_symbol="20260009",
                bank_account_id=1,
                buyer_name_cache="Acme Client a.s.",
            )
        )
        db.commit()

    client = TestClient(create_app(), base_url="https://app.example.test")
    return client, SessionLocal


def test_settings_account_edit_saves_fio_sync_configuration(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", lambda *args, **kwargs: [])

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Fio účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "fio_api",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "fio_api_token": "updated-token",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&edit_account=1#account-1"

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        assert account.payment_sync_provider == "fio_api"
        assert account.payment_sync_enabled is True
        assert account.payment_sync_auto_pair is True
        assert account.fio_api_token is not None
        assert account.fio_api_token != "updated-token"
        assert (
            decrypt_secret(account.fio_api_token, secret_key=get_settings().secret_key, purpose="fio-api-token")
            == "updated-token"
        )

    _reset_settings_and_db()


def test_settings_account_edit_rejects_invalid_fio_token(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    def _raise_invalid_token(*args, **kwargs):
        raise BankSyncError("Fio API vrátilo HTTP 403")

    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", _raise_invalid_token)

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Fio účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "fio_api",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "fio_api_token": "bad-token",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Fio API token se nepodařilo ověřit" in response.text

    _reset_settings_and_db()


def test_settings_account_edit_saves_email_sync_configuration(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Fio účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "payment_sync_email_parser": "raiffeisenbank_cz",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        assert account.payment_sync_provider == "email_bank"
        assert account.payment_sync_enabled is True
        assert account.payment_sync_email_sender_filter == "info@rb.cz"
        assert account.payment_sync_email_subject_filter == "Pohyb na účtě"
        assert account.payment_sync_email_parser == "raiffeisenbank_cz"

    _reset_settings_and_db()


def test_settings_account_edit_auto_enables_sync_for_selected_provider(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "E-mail účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_email_parser": "raiffeisenbank_cz",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        assert account.payment_sync_provider == "email_bank"
        assert account.payment_sync_enabled is True
        assert account.payment_sync_auto_pair is True

    _reset_settings_and_db()


def test_manual_bank_sync_matches_invoice_once(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankTransaction(
                provider="fio_api",
                external_id="123456789",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol="20260009",
                constant_symbol=None,
                specific_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Úhrada faktury",
                raw_payload={"id": "123456789"},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", _fake_fetch)

    first_response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert first_response.status_code == 303
    assert "/settings?info=" in first_response.headers["location"]

    second_response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert second_response.status_code == 303

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "paid"
        assert invoice.paid_on == date(2026, 3, 17)

        transactions = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert len(transactions) == 1
        assert len(payments) == 1
        assert transactions[0].matched_invoice_id == 1
        assert transactions[0].payment_id == payments[0].id

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert "Platba spárována přes Fio API" in detail_response.text
    assert "částka 7 000,00 CZK" in detail_response.text
    assert "datum připsání 2026-03-17" in detail_response.text
    assert "VS 20260009" in detail_response.text
    assert "protistrana Acme Client a.s." in detail_response.text
    assert "účet 123456789/0100" in detail_response.text
    assert "zpráva Úhrada faktury" in detail_response.text

    _reset_settings_and_db()


def test_manual_bank_sync_does_not_match_payment_older_than_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "fakturek.main.fetch_fio_transactions",
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
                message="Úhrada faktury",
                raw_payload={"id": "older-payment"},
            )
        ],
    )

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303
    retry_response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert retry_response.status_code == 303

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        transaction = db.scalar(sqlalchemy.select(BankTransaction))
        assert invoice is not None and invoice.status == "sent" and invoice.paid_on is None
        assert transaction is not None and transaction.matched_invoice_id is None and transaction.payment_id is None
        assert db.scalar(sqlalchemy.select(sqlalchemy.func.count(Payment.id))) == 0

    _reset_settings_and_db()


def test_manual_bank_sync_keeps_unmatched_payment_for_review(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankTransaction(
                provider="fio_api",
                external_id="987654321",
                booked_on=date(2026, 3, 17),
                amount_cents=699_999,
                currency="CZK",
                direction="incoming",
                variable_symbol="20260009",
                constant_symbol=None,
                specific_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Částečná úhrada",
                raw_payload={"id": "987654321"},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", _fake_fetch)

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303
    assert "/settings?info=" in response.headers["location"]

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "sent"
        assert invoice.paid_on is None

        transactions = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert len(transactions) == 1
        assert transactions[0].matched_invoice_id is None
        assert len(payments) == 0

    _reset_settings_and_db()


def test_manual_bank_sync_matches_invoice_by_number_in_message_when_vs_missing(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankTransaction(
                provider="fio_api",
                external_id="no-vs-20260009",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol=None,
                constant_symbol=None,
                specific_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Faktura 2026-0009 | Acme Client a.s.",
                raw_payload={"id": "no-vs-20260009"},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", _fake_fetch)

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303

    from fakturek.models import BankTransaction, Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.status == "paid"
        assert invoice.paid_on == date(2026, 3, 17)

        transactions = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert len(transactions) == 1
        assert transactions[0].matched_invoice_id == 1
        assert len(payments) == 1

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert "Platba spárována přes Fio API" in detail_response.text
    assert "bez variabilního symbolu" not in detail_response.text

    _reset_settings_and_db()


def test_invoice_detail_enriches_legacy_bank_sync_audit_from_bank_transaction(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import AuditLog, BankTransaction

    with SessionLocal() as db:
        db.add(
            BankTransaction(
                subject_bank_account_id=1,
                provider="fio_api",
                external_id="legacy-fio-1",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Faktura 2026-0009 | Acme Client a.s.",
                matched_invoice_id=1,
                raw_payload_json="{}",
            )
        )
        db.add(
            AuditLog(
                subject_id=1,
                action="invoice_paid_bank_sync",
                entity_type="invoice",
                entity_id=1,
                data_json='{"amount_cents": 700000, "booked_on": "2026-03-17", "currency": "CZK", "external_id": "legacy-fio-1", "from": "sent", "provider": "fio_api", "to": "paid", "variable_symbol": null}',
            )
        )
        db.commit()

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert "Platba spárována přes Fio API" in detail_response.text
    assert "protistrana Acme Client a.s." in detail_response.text
    assert "účet 123456789/0100" in detail_response.text
    assert "zpráva Faktura 2026-0009 | Acme Client a.s." in detail_response.text
    assert "bez variabilního symbolu" not in detail_response.text

    _reset_settings_and_db()


def test_invoice_detail_renders_corrected_bank_sync_match(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import AuditLog

    with SessionLocal() as db:
        db.add(
            AuditLog(
                subject_id=1,
                action="invoice_bank_sync_match_corrected",
                entity_type="invoice",
                entity_id=1,
                data_json=(
                    '{"booked_on":"2026-07-20","previous_booked_on":"2026-04-20",'
                    '"reason":"Historická platba byla omylem přiřazena k novějšímu dokladu."}'
                ),
            )
        )
        db.commit()

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert "Chybné automatické spárování opraveno" in detail_response.text
    assert "datum úhrady 2026-04-20 → 2026-07-20" in detail_response.text
    assert "Historická platba byla omylem přiřazena k novějšímu dokladu." in detail_response.text

    _reset_settings_and_db()


def test_manual_bank_sync_retries_previously_unmatched_transactions(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import BankTransaction

    with SessionLocal() as db:
        db.add(
            BankTransaction(
                subject_bank_account_id=1,
                provider="fio_api",
                external_id="older-unmatched",
                booked_on=date(2026, 3, 17),
                amount_cents=700_000,
                currency="CZK",
                direction="incoming",
                variable_symbol=None,
                counterparty_account="123456789/0100",
                counterparty_name="Acme Client a.s.",
                message="Faktura 2026-0009 | Acme Client a.s.",
                raw_payload_json="{}",
            )
        )
        db.commit()

    monkeypatch.setattr("fakturek.main.fetch_fio_transactions", lambda *args, **kwargs: [])

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303

    from fakturek.models import Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        tx = db.scalar(sqlalchemy.select(BankTransaction).where(BankTransaction.external_id == "older-unmatched"))
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert invoice is not None
        assert invoice.status == "paid"
        assert tx is not None
        assert tx.matched_invoice_id == 1
        assert len(payments) == 1

    _reset_settings_and_db()


def test_parse_raiffeisenbank_email_extracts_transaction():
    parsed = parse_raiffeisenbank_cz_email(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="501",
            external_message_id="<rb-501@example.test>",
            received_at=datetime(2026, 3, 25, 20, 57, 0),
            from_email="info@rb.cz",
            subject="Pohyb na účtě",
            body_text=(
                "Pohyb na účtě Datum a čas 25. 03. 2026 20:56 "
                "Na účet 5578244004/5500 Michal Janoušek "
                "Částka v měně účtu +2,00 CZK "
                "Kategorie pohybu Platba "
                "Typ pohybu Příchozí úhrada "
                "Z účtu 5508932004/5500 Michal Janoušek "
                "Variabilní symbol 22222222 "
                "Konstantní symbol 11111 "
                "Specifický symbol 11111 "
                "Zpráva pro příjemce Zprava pro prijemce"
            ),
            raw_headers={"From": "Raiffeisenbank <info@rb.cz>", "Subject": "Pohyb na účtě"},
        )
    )

    assert parsed.provider == "email_bank_raiffeisenbank_cz"
    assert parsed.booked_on == date(2026, 3, 25)
    assert parsed.amount_cents == 200
    assert parsed.currency == "CZK"
    assert parsed.direction == "incoming"
    assert parsed.variable_symbol == "22222222"
    assert parsed.constant_symbol == "1111"
    assert parsed.specific_symbol == "11111"
    assert parsed.counterparty_account == "5508932004/5500"
    assert parsed.counterparty_name == "Michal Janoušek"
    assert parsed.message == "Zprava pro prijemce"


def test_parse_raiffeisenbank_email_handles_nbsp_and_extra_amount_text():
    parsed = parse_raiffeisenbank_cz_email(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="502",
            external_message_id="<rb-502@example.test>",
            received_at=datetime(2026, 3, 25, 21, 1, 0),
            from_email="info@rb.cz",
            subject="Pohyb na účtě",
            body_text=(
                "Pohyb na účtě Datum a čas 25. 03. 2026 20:56 "
                "Na účet 5578244004/5500 Michal Janoušek "
                "Částka v měně účtu +57.000,00\xa0CZK zůstatek po transakci "
                "Kategorie pohybu Platba "
                "Typ pohybu Příchozí úhrada "
                "Z účtu 5508932004/5500 Michal Janoušek "
                "Variabilní symbol 20260010 "
                "Konstantní symbol 11111 "
                "Specifický symbol 11111 "
                "Zpráva pro příjemce Zprava pro prijemce"
            ),
            raw_headers={"From": "Raiffeisenbank <info@rb.cz>", "Subject": "Pohyb na účtě"},
        )
    )

    assert parsed.amount_cents == 5700000
    assert parsed.currency == "CZK"
    assert parsed.variable_symbol == "20260010"


def test_parse_raiffeisenbank_email_trims_footer_and_recovers_symbols():
    parsed = parse_raiffeisenbank_cz_email(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="503",
            external_message_id="<rb-503@example.test>",
            received_at=datetime(2026, 4, 2, 4, 54, 45),
            from_email="info@rb.cz",
            subject="Pohyb na účtě",
            body_text=(
                "Pohyb na účtě "
                "Datum a čas 02. 04. 2026 04:54 "
                "Na účet 5578244004/5500 Michal Janoušek "
                "Částka v měně účtu +260,00 CZK "
                "Kategorie pohybu Platba "
                "Typ pohybu Příchozí úhrada "
                "Z účtu 4269109023/0800 Janoušek Zdeněk "
                "Variabilní symbol 20260010 "
                "Konstantní symbol 0008 "
                "Specifický symbol 12345 "
                "Tento e-mail byl vygenerován v rámci služby Informuj mě od Raiffeisenbank a.s. "
                "V případě dotazů volejte naší Klientskou linku +420 412 440 000 nebo pište na info@rb.cz. "
                "Vaše Raiffeisenbank a.s."
            ),
            raw_headers={"From": "Raiffeisenbank <info@rb.cz>", "Subject": "Pohyb na účtě"},
        )
    )

    assert parsed.counterparty_account == "4269109023/0800"
    assert parsed.counterparty_name == "Janoušek Zdeněk"
    assert parsed.variable_symbol == "20260010"
    assert parsed.constant_symbol == "0008"
    assert parsed.specific_symbol == "12345"


def test_parse_csob_email_extracts_transaction():
    parsed = parse_csob_cz_email(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="601",
            external_message_id="<csob-601@example.test>",
            received_at=datetime(2026, 3, 25, 21, 11, 0),
            from_email="noreply@csob.cz",
            subject="Moje info - Avízo",
            body_text=(
                "Dobrý den, dne 25.3.2026 byla na účtu 291622941 zaúčtována transakce typu: Příchozí úhrada okamžitá. "
                "Parametry platby "
                "Účet 291622941/0300 "
                "Účet protistrany 5508932004/5500 "
                "Název protistrany Michal Janoušek "
                "Datum účtování 25.3.2026 "
                "Částka +1,00 CZK "
                "Variabilní symbol 12222 "
                "Konstantní symbol 141 "
                "Specifický symbol 4444 "
                "Zpráva pro příjemce Zprava pro prijemce"
            ),
            raw_headers={"From": "noreply@csob.cz", "Subject": "Moje info - Avízo"},
        )
    )

    assert parsed.provider == "email_bank_csob_cz"
    assert parsed.booked_on == date(2026, 3, 25)
    assert parsed.amount_cents == 100
    assert parsed.currency == "CZK"
    assert parsed.direction == "incoming"
    assert parsed.variable_symbol == "12222"
    assert parsed.constant_symbol == "141"
    assert parsed.specific_symbol == "4444"
    assert parsed.counterparty_account == "5508932004/5500"
    assert parsed.counterparty_name == "Michal Janoušek"
    assert parsed.message == "Zprava pro prijemce"


def test_parse_csas_email_extracts_transaction():
    parsed = parse_csas_cz_email(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="651",
            external_message_id="<csas-651@example.test>",
            received_at=datetime(2026, 3, 26, 10, 31, 0),
            from_email="ceskasporitelna@csas.cz",
            subject="Přišla platba",
            body_text=(
                "Dobrý den, pane Janoušku, "
                "na účet 4269109023/0800 právě dorazila platba ve výši 1,00 Kč. "
                "Informace o transakci "
                "Směr platby: příchozí "
                "Číslo účtu: 4269109023/0800 "
                "Číslo účtu protistrany: 5508932004/5500 "
                "Částka v měně transakce: 1,00 Kč "
                "Částka v měně účtu: 1,00 Kč "
                "Variabilní symbol: 12222 "
                "Konstantní symbol: 1222 "
                "Specifický symbol: 12222"
            ),
            raw_headers={"From": "ceskasporitelna@csas.cz", "Subject": "Přišla platba"},
        )
    )

    assert parsed.provider == "email_bank_csas_cz"
    assert parsed.booked_on == date(2026, 3, 26)
    assert parsed.amount_cents == 100
    assert parsed.currency == "CZK"
    assert parsed.direction == "incoming"
    assert parsed.variable_symbol == "12222"
    assert parsed.constant_symbol == "1222"
    assert parsed.specific_symbol == "12222"
    assert parsed.counterparty_account == "5508932004/5500"


def test_parse_fio_email_extracts_transaction():
    parsed = parse_fio_email_cz(
        ImportedBankEmail(
            provider="email_bank",
            imap_uid="701",
            external_message_id="<fio-701@example.test>",
            received_at=datetime(2026, 3, 25, 21, 12, 0),
            from_email="automat@fio.cz",
            subject="Fio banka - prijem na konte",
            body_text=(
                "Příjem na kontě: 2100087595\n"
                "Částka: 2,00\n"
                "VS: 12222\n"
                "Zpráva příjemci: ZPRAVA PRO PRIJEMCE\n"
                "Aktuální zůstatek: 4,00\n"
                "Protiúčet: 291622941/0300\n"
                "SS: 3333\n"
                "KS: 2333\n"
            ),
            raw_headers={"From": "automat@fio.cz", "Subject": "Fio banka - prijem na konte"},
        )
    )

    assert parsed.provider == "email_bank_fio_email_cz"
    assert parsed.booked_on == date(2026, 3, 25)
    assert parsed.amount_cents == 200
    assert parsed.currency == "CZK"
    assert parsed.direction == "incoming"
    assert parsed.variable_symbol == "12222"
    assert parsed.constant_symbol == "2333"
    assert parsed.specific_symbol == "3333"
    assert parsed.counterparty_account == "291622941/0300"
    assert parsed.message == "ZPRAVA PRO PRIJEMCE"


def test_settings_account_edit_rejects_email_sync_without_parser(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Fio účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "FIOBCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "payment_sync_email_parser": "pending",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400

    from fakturek.models import BankIncomingEmail, Invoice, Payment, SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        invoice = db.get(Invoice, 1)
        stored_emails = db.scalars(sqlalchemy.select(BankIncomingEmail)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert account is not None
        assert account.payment_sync_provider == "fio_api"
        assert invoice is not None
        assert invoice.status == "sent"
        assert len(payments) == 0
        assert len(stored_emails) == 0

    _reset_settings_and_db()


def test_manual_email_bank_sync_matches_raiffeisen_invoice(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Hlavní účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "RZBCCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "payment_sync_email_parser": "raiffeisenbank_cz",
        },
        follow_redirects=False,
    )

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        account.payment_sync_last_email_uid = "211"
        db.add(account)
        db.commit()

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankEmail(
                provider="email_bank",
                imap_uid="212",
                external_message_id="<rb-212@example.test>",
                received_at=datetime(2026, 3, 25, 20, 57, 0),
                from_email="info@rb.cz",
                subject="Pohyb na účtě",
                body_text=(
                    "Pohyb na účtě Datum a čas 25. 03. 2026 20:56 "
                    "Na účet 2200041594/2010 Michal Janoušek "
                    "Částka v měně účtu +7 000,00 CZK "
                    "Kategorie pohybu Platba "
                    "Typ pohybu Příchozí úhrada "
                    "Z účtu 5508932004/5500 Acme Client a.s. "
                    "Variabilní symbol 20260009 "
                    "Konstantní symbol 0308 "
                    "Specifický symbol 0000 "
                    "Zpráva pro příjemce Úhrada faktury 2026-0009"
                ),
                raw_headers={"From": "Raiffeisenbank <info@rb.cz>", "Subject": "Pohyb na účtě"},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_imap_bank_emails", _fake_fetch)

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303

    from fakturek.models import BankIncomingEmail, BankTransaction, Invoice, Payment, SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        invoice = db.get(Invoice, 1)
        stored_emails = db.scalars(sqlalchemy.select(BankIncomingEmail)).all()
        imported_rows = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert account is not None
        assert account.payment_sync_last_email_uid == "212"
        assert invoice is not None
        assert invoice.status == "paid"
        assert invoice.paid_on == date(2026, 3, 25)
        assert len(payments) == 1
        assert len(imported_rows) == 1
        assert imported_rows[0].provider == "email_bank_raiffeisenbank_cz"
        assert imported_rows[0].matched_invoice_id == 1
        assert len(stored_emails) == 1
        assert stored_emails[0].processing_status == "matched"
        assert stored_emails[0].matched_bank_transaction_id == imported_rows[0].id

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert "Platba spárována z e-mailu Raiffeisenbank" in detail_response.text
    assert "VS 20260009" in detail_response.text
    assert "protistrana Acme Client a.s." in detail_response.text

    _reset_settings_and_db()


@pytest.mark.parametrize(
    ("parser_name", "sender", "subject", "body_text", "expected_title"),
    [
        (
            "csas_cz",
            "ceskasporitelna@csas.cz",
            "Přišla platba",
            (
                "Dobrý den, pane Janoušku, "
                "na účet 2200041594/2010 právě dorazila platba ve výši 7 000,00 Kč. "
                "Informace o transakci "
                "Směr platby: příchozí "
                "Číslo účtu: 2200041594/2010 "
                "Číslo účtu protistrany: 5508932004/5500 "
                "Částka v měně transakce: 7 000,00 Kč "
                "Částka v měně účtu: 7 000,00 Kč "
                "Variabilní symbol: 20260009 "
                "Konstantní symbol: 1222 "
                "Specifický symbol: 4444"
            ),
            "Platba spárována z e-mailu České spořitelny",
        ),
        (
            "csob_cz",
            "noreply@csob.cz",
            "Moje info - Avízo",
            (
                "Dobrý den, dne 25.3.2026 byla na účtu 2200041594 zaúčtována transakce typu: Příchozí úhrada okamžitá. "
                "Parametry platby "
                "Účet 2200041594/2010 "
                "Účet protistrany 5508932004/5500 "
                "Název protistrany Acme Client a.s. "
                "Datum účtování 25.3.2026 "
                "Částka +7 000,00 CZK "
                "Variabilní symbol 20260009 "
                "Konstantní symbol 141 "
                "Specifický symbol 4444 "
                "Zpráva pro příjemce Úhrada faktury 2026-0009"
            ),
            "Platba spárována z e-mailu ČSOB",
        ),
        (
            "fio_email_cz",
            "automat@fio.cz",
            "Fio banka - prijem na konte",
            (
                "Příjem na kontě: 2200041594\n"
                "Částka: 7 000,00\n"
                "VS: 20260009\n"
                "Zpráva příjemci: Úhrada faktury 2026-0009\n"
                "Aktuální zůstatek: 4,00\n"
                "Protiúčet: 291622941/0300\n"
                "SS: 3333\n"
                "KS: 2333\n"
            ),
            "Platba spárována z e-mailu Fio banky",
        ),
    ],
)
def test_manual_email_bank_sync_matches_additional_email_parsers(
    monkeypatch,
    tmp_path,
    parser_name,
    sender,
    subject,
    body_text,
    expected_title,
):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Hlavní účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "TESTCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "payment_sync_email_parser": parser_name,
        },
        follow_redirects=False,
    )

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        account.payment_sync_last_email_uid = "811"
        db.add(account)
        db.commit()

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankEmail(
                provider="email_bank",
                imap_uid="812",
                external_message_id=f"<{parser_name}-812@example.test>",
                received_at=datetime(2026, 3, 25, 21, 15, 0),
                from_email=sender,
                subject=subject,
                body_text=body_text,
                raw_headers={"From": sender, "Subject": subject},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_imap_bank_emails", _fake_fetch)

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303

    from fakturek.models import Invoice, Payment

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        payments = db.scalars(sqlalchemy.select(Payment)).all()
        assert invoice is not None
        assert invoice.status == "paid"
        assert len(payments) == 1

    detail_response = client.get("/invoices/1")
    assert detail_response.status_code == 200
    assert expected_title in detail_response.text

    _reset_settings_and_db()


def test_manual_email_bank_sync_first_run_seeds_baseline_without_backfill(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Hlavní účet",
            "account_number": "2200041594/2010",
            "iban": "CZ0420100000002200041594",
            "bic": "TESTCZPP",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
            "payment_sync_provider": "email_bank",
            "payment_sync_enabled": "1",
            "payment_sync_auto_pair": "1",
            "payment_sync_email_parser": "csob_cz",
        },
        follow_redirects=False,
    )

    def _fake_fetch(*args, **kwargs):
        return [
            ImportedBankEmail(
                provider="email_bank",
                imap_uid="812",
                external_message_id="<csob-812@example.test>",
                received_at=datetime(2026, 3, 25, 21, 15, 0),
                from_email="noreply@csob.cz",
                subject="Moje info - Avízo",
                body_text=(
                    "Dobrý den, dne 25.3.2026 byla na účtu 2200041594 zaúčtována transakce typu: Příchozí úhrada okamžitá. "
                    "Parametry platby "
                    "Účet 2200041594/2010 "
                    "Účet protistrany 5508932004/5500 "
                    "Název protistrany Acme Client a.s. "
                    "Datum účtování 25.3.2026 "
                    "Částka +7 000,00 CZK "
                    "Variabilní symbol 20260009 "
                    "Konstantní symbol 141 "
                    "Specifický symbol 4444 "
                    "Zpráva pro příjemce Úhrada faktury 2026-0009"
                ),
                raw_headers={"From": "noreply@csob.cz", "Subject": "Moje info - Avízo"},
            )
        ]

    monkeypatch.setattr("fakturek.main.fetch_imap_bank_emails", _fake_fetch)

    response = client.post("/settings/accounts/1/sync", follow_redirects=False)
    assert response.status_code == 303
    assert "Párování je připravené od této chvíle." in unquote(response.headers["location"])

    from fakturek.models import BankIncomingEmail, BankTransaction, Invoice, Payment, SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        invoice = db.get(Invoice, 1)
        stored_emails = db.scalars(sqlalchemy.select(BankIncomingEmail)).all()
        imported_rows = db.scalars(sqlalchemy.select(BankTransaction)).all()
        payments = db.scalars(sqlalchemy.select(Payment)).all()

        assert account is not None
        assert account.payment_sync_last_email_uid == "812"
        assert invoice is not None
        assert invoice.status == "sent"
        assert len(stored_emails) == 0
        assert len(imported_rows) == 0
        assert len(payments) == 0

    _reset_settings_and_db()

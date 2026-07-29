from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path

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
    db_path = tmp_path / "phase49.sqlite3"
    import_root = tmp_path / "imports"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(import_root))
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, InvoiceSeries, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Jan Novák",
                email="owner@example.test",
                street="Testovací 1",
                city="Praha",
                zip="110 00",
                country="CZ",
                ico="12345678",
                default_currency="CZK",
                default_invoice_footer_mode="custom",
                default_invoice_footer_text="Fyzická osoba zapsaná v živnostenském rejstříku.",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="SILVER LANE ALFA s.r.o.",
                email="billing@example.test",
                ico="24843032",
                street="Za Hl\u00eddkovem 680/12",
                city="Praha",
                zip="16900",
                country="CZ",
                fixed_variable_symbol="1234567890",
            )
        )
        db.add(
            InvoiceSeries(
                id=1,
                subject_id=1,
                name="default",
                prefix="2026-",
                pad_length=4,
                last_counter=0,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal, import_root


def _create_import_run(SessionLocal, import_root: Path, *, filename: str, payload: bytes, mime_type: str) -> int:
    from fakturek.models import ImportRun

    sha256_hex = hashlib.sha256(payload).hexdigest()
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source="fakturoid",
            status="uploaded",
            file_name=filename,
            file_path="",
            file_sha256=sha256_hex,
            file_size_bytes=len(payload),
            mime_type=mime_type,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        rel = Path(f"subject-1/run-{int(run.id)}/{filename}")
        full_path = import_root / rel
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(payload)

        run.file_path = rel.as_posix()
        db.add(run)
        db.commit()
        return int(run.id)


def test_contact_fixed_variable_symbol_is_used_for_new_invoice(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    contact_edit = client.get("/contacts/1/edit")
    assert contact_edit.status_code == 200
    assert 'name="fixed_variable_symbol"' in contact_edit.text
    assert 'value="1234567890"' in contact_edit.text

    contact_detail = client.get("/contacts/1")
    assert contact_detail.status_code == 200
    assert "Pevný VS" in contact_detail.text
    assert "1234567890" in contact_detail.text

    response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "series_id": "1",
            "issue_date": "2026-03-14",
            "due_date": "2026-03-28",
            "due_term": "14",
            "currency": "CZK",
            "payment_method": "bank_transfer",
            "notes": "",
            "item_description": ["Správa infrastruktury"],
            "item_quantity": ["1"],
            "item_unit_price": ["7000.00"],
            "item_vat_rate": ["0"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/1"

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert invoice.variable_symbol == "1234567890"
        assert invoice.footer_mode == "custom"
        assert invoice.footer_text == "Fyzická osoba zapsaná v živnostenském rejstříku."

    _reset_settings_and_db()


def test_settings_issuer_can_store_default_footer_preferences(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200

    response = client.post(
        "/settings/issuer",
        data={
            "name": "Jan Novák",
            "email": "owner@example.test",
            "phone": "",
            "street": "Testovací 1",
            "city": "Praha",
            "zip": "110 00",
            "country": "CZ",
            "ico": "12345678",
            "dic": "",
            "default_currency": "CZK",
            "default_invoice_style": "modern",
            "default_invoice_footer_mode": "association_register",
            "default_invoice_footer_text": "Ignorovat",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    from fakturek.models import Subject

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        assert subject.default_invoice_style == "modern"
        assert subject.default_invoice_footer_mode == "association_register"

    refreshed = client.get("/settings")
    assert refreshed.status_code == 200

    _reset_settings_and_db()


def test_process_import_run_preserves_variable_symbol_from_xml(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    xml_payload = (
        """<?xml version="1.0" encoding="UTF-8"?>
<invoices>
  <invoice>
    <id>49</id>
    <number>2026-0049</number>
    <variable_symbol>777000111</variable_symbol>
    <status>sent</status>
    <issued_on>2026-03-10</issued_on>
    <due_on>2026-03-24</due_on>
    <currency>CZK</currency>
    <total>7000.0</total>
    <client_name>Client Import s.r.o.</client_name>
    <client_city>Praha</client_city>
    <client_country>CZ</client_country>
    <client_registration_no>12345678</client_registration_no>
    <lines>
      <line>
        <name>Importovaná služba</name>
        <quantity>1</quantity>
        <unit_price>7000</unit_price>
        <vat_rate>0</vat_rate>
        <total_price_without_vat>7000</total_price_without_vat>
        <total_vat>0</total_vat>
      </line>
    </lines>
  </invoice>
</invoices>
"""
    ).encode("utf-8")

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturoid-invoices.xml",
        payload=xml_payload,
        mime_type="application/xml",
    )

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import ImportRun, Invoice

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

        invoice = db.query(Invoice).filter(Invoice.number == "2026-0049").one()
        assert summary["invoices"]["imported"] == 1
        assert invoice.variable_symbol == "777000111"
        assert invoice.issue_date == date(2026, 3, 10)

    _reset_settings_and_db()

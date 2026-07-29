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


SAMPLE_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<invoices>
  <invoice>
    <id>27</id>
    <number>2023-0021</number>
    <status>sent</status>
    <issued_on>2023-11-30</issued_on>
    <due_on>2023-12-14</due_on>
    <currency>CZK</currency>
    <total>11000.0</total>
    <note>Fakturujeme Vám následující položky</note>
    <private_note>internal</private_note>
    <your_name>Pavel Šnajdr</your_name>
    <your_street>Čápkova 13/5</your_street>
    <your_city>Brno</your_city>
    <your_zip>60200</your_zip>
    <your_country>CZ</your_country>
    <your_registration_no>03485714</your_registration_no>
    <subject_id>16</subject_id>
    <client_name>Apple Czech s.r.o.</client_name>
    <client_street>Klimentská 1216/46</client_street>
    <client_city>Praha</client_city>
    <client_zip>11000</client_zip>
    <client_country>CZ</client_country>
    <client_registration_no>28897501</client_registration_no>
    <client_vat_no>CZ28897501</client_vat_no>
    <lines>
      <line>
        <id>46</id>
        <name>Grafická karta</name>
        <quantity>1.0</quantity>
        <unit_price>8264.0</unit_price>
        <vat_rate>21</vat_rate>
        <total_price_without_vat>8264.0</total_price_without_vat>
        <total_vat>1736.0</total_vat>
      </line>
    </lines>
  </invoice>
</invoices>
"""
).encode("utf-8")


SAMPLE_CONTACTS_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<contacts>
  <contact>
    <id>16</id>
    <company_name>Apple Czech s.r.o.</company_name>
    <email>info@example.com</email>
    <street>Klimentská 1216/46</street>
    <city>Praha</city>
    <postal_code>11000</postal_code>
    <country>CZ</country>
    <registration_no>28897501</registration_no>
    <vat_no>CZ28897501</vat_no>
    <telephone>+420123</telephone>
  </contact>
</contacts>
"""
).encode("utf-8")

POHODA_IMPORT_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<dat:dataPack xmlns:dat="http://www.stormware.cz/schema/version_2/data.xsd"
  xmlns:inv="http://www.stormware.cz/schema/version_2/invoice.xsd"
  xmlns:typ="http://www.stormware.cz/schema/version_2/type.xsd">
  <dat:dataPackItem id="invoice-1">
    <inv:invoice>
      <inv:invoiceHeader>
        <inv:number><typ:numberRequested>2026-0400</typ:numberRequested></inv:number>
        <inv:symVar>20260400</inv:symVar>
        <inv:date>2026-05-20</inv:date>
        <inv:dateDue>2026-05-27</inv:dateDue>
        <inv:text>POHODA import</inv:text>
        <inv:partnerIdentity>
          <typ:address>
            <typ:company>POHODA Client s.r.o.</typ:company>
            <typ:street>Karlova 1</typ:street>
            <typ:city>Praha</typ:city>
            <typ:zip>11000</typ:zip>
            <typ:ico>87654321</typ:ico>
          </typ:address>
        </inv:partnerIdentity>
      </inv:invoiceHeader>
      <inv:invoiceDetail>
        <inv:invoiceItem>
          <inv:text>Consulting</inv:text>
          <inv:quantity>2</inv:quantity>
          <inv:unit>hour</inv:unit>
          <inv:rateVAT>0</inv:rateVAT>
          <inv:homeCurrency>
            <typ:unitPrice>50.00</typ:unitPrice>
            <typ:price>100.00</typ:price>
            <typ:priceSum>100.00</typ:priceSum>
          </inv:homeCurrency>
        </inv:invoiceItem>
      </inv:invoiceDetail>
      <inv:invoiceSummary>
        <inv:homeCurrency>
          <typ:priceNone>100.00</typ:priceNone>
        </inv:homeCurrency>
      </inv:invoiceSummary>
    </inv:invoice>
  </dat:dataPackItem>
</dat:dataPack>
"""
).encode("utf-8")


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "phase37.sqlite3"
    import_root = tmp_path / "imports"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(import_root))
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Subject

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
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal, import_root


def _create_import_run(SessionLocal, import_root: Path, *, filename: str, payload: bytes, source: str = "fakturoid", summary_json: str | None = None) -> int:
    from fakturek.models import ImportRun

    sha256_hex = hashlib.sha256(payload).hexdigest()
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=source,
            status="uploaded",
            file_name=filename,
            file_path="",
            file_sha256=sha256_hex,
            file_size_bytes=len(payload),
            mime_type="application/xml",
            summary_json=summary_json,
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


def test_import_process_creates_public_link_and_preview_actions(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(SessionLocal, import_root, filename="invoices.xml", payload=SAMPLE_XML)

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import ImportRun, Invoice, InvoiceParty, Subject

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

        invoice = db.query(Invoice).order_by(Invoice.id.asc()).first()
        subject = db.get(Subject, 1)
        assert invoice is not None
        assert subject is not None
        assert invoice.public_token
        assert subject.public_username
        assert summary["invoices"]["imported"] == 1
        assert summary["invoices"]["public_links_created"] == 1
        seller = db.query(InvoiceParty).filter_by(invoice_id=invoice.id, role="seller").one()
        assert seller.name == "Pavel Šnajdr"
        assert seller.street == "Čápkova 13/5"
        assert seller.city == "Brno"
        assert seller.zip == "60200"
        assert seller.ico == "03485714"

        public_path = f"/{subject.public_username}/i/{invoice.public_token}/{invoice.number}"
        invoice_id = int(invoice.id)

    internal_preview = client.get(f"/invoices/{invoice_id}/print")
    assert internal_preview.status_code == 200
    assert "Otevřít PDF" in internal_preview.text
    assert "Stáhnout PDF" in internal_preview.text

    public_preview = client.get(public_path)
    assert public_preview.status_code == 200
    assert "Otevřít PDF" in public_preview.text
    assert "Stáhnout PDF" in public_preview.text

    _reset_settings_and_db()


def test_import_detail_shows_preview_and_csv_mapping(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    csv_payload = (
        "Company,E-mail,Street,Postal,CityName,CountryCode,CompanyID,TaxID,PhoneNumber,Ref\n"
        "Red Com,compras@example.test,Avenida Paulista,01311-200,Sao Paulo,BR,39357251000163,BR39357251000163,+5511999,4470\n"
    ).encode("utf-8")
    run_id = _create_import_run(SessionLocal, import_root, filename="contacts.csv", payload=csv_payload)

    detail = client.get(f"/imports/{run_id}")
    assert detail.status_code == 200
    assert "Co se stane při importu" in detail.text
    assert "Sloupce kontaktů" in detail.text
    assert "Company" in detail.text
    assert "Uložit mapping" in detail.text

    saved = client.post(
        f"/imports/{run_id}/config",
        data={
            "map_name": "Company",
            "map_email": "E-mail",
            "map_street": "Street",
            "map_zip": "Postal",
            "map_city": "CityName",
            "map_country": "CountryCode",
            "map_ico": "CompanyID",
            "map_dic": "TaxID",
            "map_phone": "PhoneNumber",
            "map_fixed_variable_symbol": "Ref",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    processed = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert processed.status_code == 303

    from fakturek.models import Contact

    with SessionLocal() as db:
        contact = db.query(Contact).filter(Contact.name == "Red Com").one()
        assert contact.email == "compras@example.test"
        assert contact.fixed_variable_symbol == "4470"

    _reset_settings_and_db()


def test_contact_import_backfills_fixed_variable_symbol_for_existing_import_map(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.importing import ensure_import_map
    from fakturek.models import Contact, ImportRun
    from fakturek.fakturoid_import import process_import_run

    payload = (
        """<?xml version="1.0" encoding="UTF-8"?>
<subjects>
  <subject>
    <id>8636157</id>
    <company_name>hkfree.org z.s.</company_name>
    <registration_no>26659573</registration_no>
    <variable_symbol>3533</variable_symbol>
  </subject>
</subjects>
"""
    ).encode("utf-8")

    with SessionLocal() as db:
        contact = Contact(
            id=1,
            subject_id=1,
            name="hkfree.org z.s.",
            ico="26659573",
            external_source="fakturoid",
            external_id="8636157",
            fixed_variable_symbol=None,
        )
        db.add(contact)
        db.flush()
        ensure_import_map(
            db,
            subject_id=1,
            source="fakturoid",
            entity_type="contact",
            external_id="8636157",
            internal_id=int(contact.id),
        )
        db.commit()

    run_id = _create_import_run(SessionLocal, import_root, filename="contacts.xml", payload=payload)

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

    with SessionLocal() as db:
        contact = db.get(Contact, 1)
        assert contact is not None
        assert contact.fixed_variable_symbol == "3533"
        assert summary["contacts"]["created"] == 0
        assert summary["contacts"]["reused"] == 1
        assert summary["contacts"]["skipped_existing"] == 0


def test_duplicate_import_backfills_public_link_for_existing_invoice(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Contact, ImportMap, ImportRun, Invoice, Subject

    with SessionLocal() as db:
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Apple Czech s.r.o.",
                email="billing@example.test",
                city="Praha",
                country="CZ",
                ico="28897501",
            )
        )
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                number="2023-0021",
                status="sent",
                issue_date=date(2023, 11, 30),
                due_date=date(2023, 12, 14),
                currency="CZK",
                notes="Původní import",
                buyer_name_cache="Apple Czech s.r.o.",
                buyer_registration_no_cache="28897501",
                total_cents=1100000,
                public_token=None,
            )
        )
        db.add(
            ImportMap(
                subject_id=1,
                source="fakturoid",
                entity_type="invoice",
                external_id="27",
                internal_id=1,
            )
        )
        db.commit()

    run_id = _create_import_run(SessionLocal, import_root, filename="invoices.xml", payload=SAMPLE_XML)

    from fakturek.fakturoid_import import process_import_run

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

        invoice = db.get(Invoice, 1)
        subject = db.get(Subject, 1)
        assert invoice is not None
        assert subject is not None
        assert invoice.public_token
        assert subject.public_username
        assert summary["invoices"]["imported"] == 0
        assert summary["invoices"]["skipped_existing"] == 1
        assert summary["invoices"]["public_links_backfilled"] == 1

        public_path = f"/{subject.public_username}/i/{invoice.public_token}/{invoice.number}"

    public_preview = client.get(public_path)
    assert public_preview.status_code == 200
    assert "Stáhnout PDF" in public_preview.text

    _reset_settings_and_db()


def test_import_detail_preview_supports_pohoda_xml(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="pohoda.xml",
        payload=POHODA_IMPORT_XML,
        source="pohoda_xml",
    )

    detail = client.get(f"/imports/{run_id}")
    assert detail.status_code == 200
    assert "POHODA XML" in detail.text
    assert "Přečíslované faktury" in detail.text
    assert "Konflikt čísla faktury" in detail.text
    _reset_settings_and_db()


def test_process_import_run_can_renumber_conflicting_invoice(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import Contact, ImportRun, Invoice, InvoiceItem

    with SessionLocal() as db:
        contact = Contact(subject_id=1, name="Existing customer")
        db.add(contact)
        db.flush()
        db.add(
            Invoice(
                subject_id=1,
                contact_id=int(contact.id),
                number="2026-0400",
                status="issued",
                issue_date=date(2026, 5, 1),
                due_date=date(2026, 5, 8),
                currency="CZK",
                total_cents=5000,
            )
        )
        db.commit()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="pohoda.xml",
        payload=POHODA_IMPORT_XML,
        source="pohoda_xml",
        summary_json='{"config":{"invoice_number_conflict_mode":"renumber"}}',
    )

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        invoices = db.query(Invoice).order_by(Invoice.id.asc()).all()
        numbers = [row.number for row in invoices]
        assert "2026-0400" in numbers
        assert "2026-0401" in numbers
        created = db.query(Invoice).filter(Invoice.number == "2026-0401").one()
        item = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == int(created.id)).one()
        assert item.unit == "hour"
        assert summary["invoices"]["renumbered"] == 1
        assert summary["invoices"]["imported"] == 1
        assert summary["detected"]["xml_format"] == "pohoda_xml"

    _reset_settings_and_db()


def test_import_process_creates_contacts_from_xml(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(SessionLocal, import_root, filename="contacts.xml", payload=SAMPLE_CONTACTS_XML)

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import Contact, ImportRun

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

        contact = db.query(Contact).order_by(Contact.id.asc()).first()
        assert contact is not None
        assert contact.name == "Apple Czech s.r.o."
        assert contact.email == "info@example.com"
        assert contact.ico == "28897501"
        assert summary["contacts"]["parsed"] == 1
        assert summary["contacts"]["created"] == 1
        assert summary["invoices"]["parsed"] == 0

    _reset_settings_and_db()

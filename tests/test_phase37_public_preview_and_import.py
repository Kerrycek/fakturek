from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

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


def _fakturek_xml_v1_payload(*, generated_at: str = "2026-09-19T10:00:00+00:00") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fakturek_export kind="invoice_export" format="fakturek_invoice_export" version="1"
  origin_subject_id="44" generated_at_utc="{generated_at}">
  <invoices count="1">
    <invoice id="77" number="2026-NATIVE-77" document_type="quote" status="cancelled">
      <issue_date>2026-09-01</issue_date><taxable_supply_date>2026-08-31</taxable_supply_date>
      <due_date>2026-09-15</due_date><paid_on></paid_on><sent_at>2026-09-02T08:30:00+02:00</sent_at>
      <currency>EUR</currency><variable_symbol>77001</variable_symbol><payment_method>bank_transfer</payment_method>
      <total_cents>12000</total_cents><discount_cents>100</discount_cents><rounding_adjustment_cents>0</rounding_adjustment_cents>
      <notes>Portable note</notes><internal_notes>Portable private note</internal_notes>
      <contact><id>88</id><name>Portable Buyer s.r.o.</name><email>buyer@example.test</email>
        <phone>+420123456789</phone><street>Portable 1</street><city>Praha</city><zip>11000</zip>
        <country>CZ</country><ico>11223344</ico><dic>CZ11223344</dic></contact>
      <bank_account><label>EUR účet</label><number>123456789/0100</number>
        <iban>CZ6508000000192000145399</iban><bic>GIBACZPX</bic><country>CZ</country></bank_account>
      <items count="1"><item line_no="1"><description>Portable service</description><quantity>1</quantity><unit>ks</unit>
        <unit_price_cents>10000</unit_price_cents><vat_rate>21</vat_rate><line_net_cents>10000</line_net_cents>
        <line_vat_cents>2100</line_vat_cents><line_total_cents>12100</line_total_cents></item></items>
    </invoice>
  </invoices>
</fakturek_export>
""".encode("utf-8")


def _fakturek_xml_v2_payload(*, generated_at: str = "2026-09-19T10:00:00+00:00") -> bytes:
    root = ET.fromstring(_fakturek_xml_v1_payload(generated_at=generated_at))
    root.attrib["version"] = "2"
    invoice = root.find("./invoices/invoice")
    assert invoice is not None
    invoice_paid_on = invoice.find("./paid_on")
    assert invoice_paid_on is not None
    invoice_paid_on.text = "2026-09-06"
    payments = ET.SubElement(invoice, "payments", {"count": "2"})
    first = ET.SubElement(payments, "payment", {"id": "901"})
    ET.SubElement(first, "paid_on").text = "2026-09-03"
    ET.SubElement(first, "amount_cents").text = "12100"
    ET.SubElement(first, "note").text = "První platba"
    second = ET.SubElement(payments, "payment", {"id": "902"})
    ET.SubElement(second, "paid_on").text = "2026-09-04"
    ET.SubElement(second, "amount_cents").text = "-100"
    ET.SubElement(second, "note").text = "Korekce"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _malformed_fakturek_xml_v2_payload(case: str) -> bytes:
    root = ET.fromstring(_fakturek_xml_v2_payload())
    payments = root.find("./invoices/invoice/payments")
    assert payments is not None
    rows = payments.findall("./payment")
    assert len(rows) == 2
    if case == "count":
        payments.attrib["count"] = "3"
    elif case == "duplicate_id":
        rows[1].attrib["id"] = rows[0].attrib["id"]
    elif case == "date":
        paid_on = rows[0].find("./paid_on")
        assert paid_on is not None
        paid_on.text = "not-a-date"
    elif case == "range":
        amount = rows[0].find("./amount_cents")
        assert amount is not None
        amount.text = "2147483648"
    elif case == "note":
        note = rows[0].find("./note")
        assert note is not None
        note.text = "x" * 256
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"Unknown malformed case: {case}")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _fakturek_credit_pair_payload(*, over_credit: bool = False) -> bytes:
    credit_unit = -15000 if over_credit else -5000
    credit_net = credit_unit
    credit_vat = -3150 if over_credit else -1050
    credit_total = credit_net + credit_vat

    def invoice_xml(
        *,
        invoice_id: int,
        number: str,
        document_type: str,
        status: str,
        unit_price: int,
        net: int,
        vat: int,
        total: int,
        source_number: str = "",
        source_id: str = "",
    ) -> str:
        return f"""
    <invoice id="{invoice_id}" number="{number}" document_type="{document_type}" status="{status}">
      <issue_date>2026-09-01</issue_date><taxable_supply_date>2026-09-01</taxable_supply_date>
      <due_date>2026-09-15</due_date><currency>CZK</currency>
      <total_cents>{total}</total_cents><discount_cents>0</discount_cents><rounding_adjustment_cents>0</rounding_adjustment_cents>
      <source_invoice_number>{source_number}</source_invoice_number>
      <source_invoice_id>{source_id}</source_invoice_id>
      <contact><id>88</id><name>Credit Buyer</name><country>CZ</country></contact>
      <items count="1"><item line_no="1"><description>Service</description><quantity>1</quantity>
        <unit_price_cents>{unit_price}</unit_price_cents><vat_rate>21</vat_rate>
        <line_net_cents>{net}</line_net_cents><line_vat_cents>{vat}</line_vat_cents>
        <line_total_cents>{total}</line_total_cents></item></items>
    </invoice>"""

    # Credit note intentionally comes first; the importer must restore the
    # dependency rather than relying on the file's display order.
    credit = invoice_xml(
        invoice_id=78,
        number="2026-CREDIT-78",
        document_type="credit_note",
        status="issued",
        unit_price=credit_unit,
        net=credit_net,
        vat=credit_vat,
        total=credit_total,
        source_number="2026-ORIGINAL-77",
        source_id="77",
    )
    original = invoice_xml(
        invoice_id=77,
        number="2026-ORIGINAL-77",
        document_type="invoice",
        status="paid",
        unit_price=10000,
        net=10000,
        vat=2100,
        total=12100,
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fakturek_export kind="invoice_export" format="fakturek_invoice_export" version="1"
  origin_subject_id="44" generated_at_utc="2026-09-19T12:00:00+00:00">
  <invoices count="2">{credit}{original}
  </invoices>
</fakturek_export>
""".encode("utf-8")


def _fakturek_credit_pair_v2_payload() -> bytes:
    root = ET.fromstring(_fakturek_credit_pair_payload())
    root.attrib["version"] = "2"
    for invoice in root.findall("./invoices/invoice"):
        payments = ET.SubElement(invoice, "payments", {"count": "0"})
        if invoice.attrib.get("document_type") != "invoice":
            continue
        payments.attrib["count"] = "1"
        payment = ET.SubElement(payments, "payment", {"id": "990"})
        ET.SubElement(payment, "paid_on").text = "2026-09-05"
        ET.SubElement(payment, "amount_cents").text = "12100"
        ET.SubElement(payment, "note").text = "Úhrada původní faktury"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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


def test_fakturek_xml_v1_preview_process_and_cross_source_replay(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = _fakturek_xml_v1_payload()
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v1.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import Contact, ImportMap, ImportRun, Invoice, InvoiceItem, InvoiceParty

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["detected"]["xml_format"] == "fakturek_xml_v1"
        assert preview["invoices"]["parsed"] == 1
        assert preview["invoices"]["will_import"] == 1

        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["detected"]["xml_format"] == "fakturek_xml_v1"
        assert summary["invoices"]["imported"] == 1

        invoice = db.query(Invoice).one()
        contact = db.query(Contact).one()
        item = db.query(InvoiceItem).one()
        seller = db.query(InvoiceParty).filter_by(invoice_id=invoice.id, role="seller").one()
        assert invoice.number == "2026-NATIVE-77"
        assert invoice.document_type == "quote"
        assert invoice.status == "cancelled"
        assert invoice.taxable_supply_date == date(2026, 8, 31)
        assert invoice.currency == "EUR"
        assert invoice.variable_symbol == "77001"
        assert invoice.discount_cents == 100
        assert invoice.rounding_adjustment_cents == 0
        assert invoice.total_cents == 12000
        assert invoice.bank_account_label == "EUR účet"
        assert invoice.bank_account_iban == "CZ6508000000192000145399"
        assert contact.name == "Portable Buyer s.r.o."
        assert contact.phone == "+420123456789"
        assert item.description == "Portable service"
        assert item.line_total_cents == 12100
        assert seller.name == "Test subject"
        assert db.query(ImportMap).filter_by(
            source="fakturek_xml_v1",
            entity_type="invoice",
            external_id="v1:44:invoice:77",
        ).count() == 1

    replay_payload = _fakturek_xml_v1_payload(generated_at="2026-09-19T11:00:00+00:00")
    replay_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v1-replay.xml",
        payload=replay_payload,
        source="fakturoid",
    )
    with SessionLocal() as db:
        replay = db.get(ImportRun, replay_id)
        preview = preview_import_run(db, run=replay, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["already_imported"] == 1
        summary = process_import_run(db, run=replay, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["invoices"]["imported"] == 0
        assert summary["invoices"]["skipped_existing"] == 1
        assert db.query(Invoice).count() == 1
        assert db.query(Contact).count() == 1

    _reset_settings_and_db()


def test_fakturek_xml_v2_preview_process_and_cross_source_payment_replay(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = _fakturek_xml_v2_payload()
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import ImportMap, ImportRun, Invoice, Payment

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["detected"]["xml_format"] == "fakturek_xml_v2"
        assert preview["invoices"]["will_import"] == 1
        assert preview["payments"] == {
            "parsed": 2,
            "will_create": 2,
            "will_reuse": 0,
            "will_skip": 0,
            "errors": [],
        }

        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["detected"]["xml_format"] == "fakturek_xml_v2"
        assert summary["invoices"]["imported"] == 1
        assert summary["payments"] == {
            "parsed": 2,
            "created": 2,
            "reused": 0,
            "skipped": 0,
            "errors": [],
        }

        invoice = db.query(Invoice).one()
        payments = db.query(Payment).order_by(Payment.id.asc()).all()
        assert invoice.status == "cancelled"
        assert invoice.paid_on == date(2026, 9, 6)
        assert [(row.paid_on, row.amount_cents, row.note) for row in payments] == [
            (date(2026, 9, 3), 12_100, "První platba"),
            (date(2026, 9, 4), -100, "Korekce"),
        ]
        assert db.query(ImportMap).filter_by(
            subject_id=1,
            source="fakturek_xml_v2",
            entity_type="payment",
        ).count() == 2

        # A replay must not overwrite user edits to an imported payment.
        payments[0].amount_cents = 12_000
        payments[0].note = "Uživatelská úprava"
        db.commit()

    replay_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2-replay.xml",
        payload=_fakturek_xml_v2_payload(generated_at="2026-09-19T11:00:00+00:00"),
        source="fakturoid",
    )
    with SessionLocal() as db:
        replay = db.get(ImportRun, replay_id)
        preview = preview_import_run(db, run=replay, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["already_imported"] == 1
        assert preview["payments"]["will_reuse"] == 2
        assert preview["payments"]["will_create"] == 0
        assert preview["payments"]["will_skip"] == 0

        summary = process_import_run(
            db,
            run=replay,
            subject_id=1,
            import_storage_root=import_root,
        )
        db.commit()
        assert summary["invoices"]["imported"] == 0
        assert summary["payments"]["created"] == 0
        assert summary["payments"]["reused"] == 2
        assert db.query(Invoice).count() == 1
        assert db.query(Payment).count() == 2
        first = db.query(Payment).order_by(Payment.id.asc()).first()
        assert first.amount_cents == 12_000
        assert first.note == "Uživatelská úprava"

    _reset_settings_and_db()


def test_fakturek_xml_v2_preview_counts_payments_skipped_with_invoice_conflict(
    monkeypatch,
    tmp_path,
):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    v1_run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v1.xml",
        payload=_fakturek_xml_v1_payload(),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import ImportRun, Invoice, Payment

    with SessionLocal() as db:
        v1_run = db.get(ImportRun, v1_run_id)
        process_import_run(db, run=v1_run, subject_id=1, import_storage_root=import_root)
        db.commit()

    v2_run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2.xml",
        payload=_fakturek_xml_v2_payload(),
        source="fakturoid",
    )
    with SessionLocal() as db:
        v2_run = db.get(ImportRun, v2_run_id)
        preview = preview_import_run(db, run=v2_run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["number_conflicts"] == 1
        assert preview["payments"]["will_skip"] == 2
        assert preview["payments"]["will_create"] == 0

        summary = process_import_run(db, run=v2_run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["payments"]["skipped"] == 2
        assert summary["payments"]["created"] == 0
        assert db.query(Invoice).count() == 1
        assert db.query(Payment).count() == 0

    _reset_settings_and_db()


@pytest.mark.parametrize("case", ["count", "duplicate_id", "date", "range", "note"])
def test_invalid_fakturek_xml_v2_payment_document_writes_nothing(monkeypatch, tmp_path, case):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename=f"fakturek-v2-{case}.xml",
        payload=_malformed_fakturek_xml_v2_payload(case),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import Contact, ImportMap, ImportRun, Invoice, Payment

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["invoices"]["imported"] == 0
        assert summary["invoices"]["errors"]
        assert db.query(Invoice).count() == 0
        assert db.query(Payment).count() == 0
        assert db.query(Contact).count() == 0
        assert db.query(ImportMap).count() == 0

    _reset_settings_and_db()


def test_fakturek_xml_v2_rejects_cross_tenant_import_run(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2.xml",
        payload=_fakturek_xml_v2_payload(),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import Contact, ImportRun, Invoice, Payment

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        with pytest.raises(ValueError, match="nepatří"):
            preview_import_run(db, run=run, subject_id=2, import_storage_root=import_root)
        with pytest.raises(ValueError, match="nepatří"):
            process_import_run(db, run=run, subject_id=2, import_storage_root=import_root)
        db.rollback()
        assert db.query(Invoice).count() == 0
        assert db.query(Payment).count() == 0
        assert db.query(Contact).count() == 0

    _reset_settings_and_db()


def test_fakturek_xml_v2_payment_import_maps_are_tenant_scoped(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Contact, ImportMap, Invoice, InvoiceItem, Payment, Subject

    with SessionLocal() as db:
        db.add(Subject(id=2, name="Other tenant", email="other@example.test"))
        db.add(Contact(id=200, subject_id=2, name="Other buyer"))
        db.add(
            Invoice(
                id=200,
                subject_id=2,
                contact_id=200,
                number="OTHER-1",
                status="paid",
                issue_date=date(2026, 9, 1),
                due_date=date(2026, 9, 15),
                currency="EUR",
                total_cents=12_100,
            )
        )
        db.add(
            InvoiceItem(
                invoice_id=200,
                description="Other item",
                quantity=1,
                unit_price_cents=10_000,
                vat_rate=21,
                line_net_cents=10_000,
                line_vat_cents=2_100,
                line_total_cents=12_100,
                sort_order=0,
            )
        )
        db.add(
            Payment(
                id=200,
                invoice_id=200,
                paid_on=date(2026, 9, 3),
                amount_cents=12_100,
                note="Other tenant payment",
            )
        )
        db.add(
            ImportMap(
                subject_id=2,
                source="fakturek_xml_v2",
                entity_type="payment",
                external_id="v2:44:payment:901",
                internal_id=200,
            )
        )
        db.commit()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2.xml",
        payload=_fakturek_xml_v2_payload(),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import ImportRun

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["payments"]["will_create"] == 2
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["payments"]["created"] == 2
        subject_1_payment_ids = [
            payment.id
            for payment in db.query(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .filter(Invoice.subject_id == 1)
            .order_by(Payment.id.asc())
            .all()
        ]
        assert len(subject_1_payment_ids) == 2
        assert 200 not in subject_1_payment_ids
        assert db.get(Payment, 200).note == "Other tenant payment"

    _reset_settings_and_db()


def test_fakturek_xml_v2_preview_routes_payment_map_errors_to_payments(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import ImportMap

    with SessionLocal() as db:
        db.add(
            ImportMap(
                subject_id=1,
                source="fakturek_xml_v2",
                entity_type="payment",
                external_id="v2:44:payment:901",
                internal_id=999_999,
            )
        )
        db.commit()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2.xml",
        payload=_fakturek_xml_v2_payload(),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run
    from fakturek.models import ImportRun

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["errors"] == []
        assert preview["payments"]["errors"]
        assert "integrity faktur nebo plateb" in preview["ready_note"]
        assert "dobropis" not in preview["ready_note"]

    detail = client.get(f"/imports/{run_id}")
    assert detail.status_code == 200
    assert "Chyby u plateb" in detail.text
    assert "Mapování platby Fakturek XML" in detail.text

    _reset_settings_and_db()


def test_fakturek_xml_v2_preview_rejects_stale_invoice_map_without_payments(
    monkeypatch,
    tmp_path,
):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    root = ET.fromstring(_fakturek_xml_v2_payload())
    payments = root.find("./invoices/invoice/payments")
    assert payments is not None
    payments.clear()
    payments.attrib["count"] = "0"
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    from fakturek.models import ImportMap

    with SessionLocal() as db:
        db.add(
            ImportMap(
                subject_id=1,
                source="fakturek_xml_v2",
                entity_type="invoice",
                external_id="v2:44:invoice:77",
                internal_id=999_999,
            )
        )
        db.commit()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-v2-no-payments.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run
    from fakturek.models import ImportRun

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["payments"]["parsed"] == 0
        assert preview["payments"]["errors"] == []
        assert preview["invoices"]["errors"]
        assert "integrity faktur nebo plateb" in preview["ready_note"]

    _reset_settings_and_db()


def test_invalid_fakturek_xml_fails_closed_without_contact_or_invoice_writes(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = _fakturek_xml_v1_payload().replace(
        b"<total_cents>12000",
        b"<total_cents>11999",
    )
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="tampered-fakturek-v1.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import Contact, ImportRun, Invoice

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["invoices"]["imported"] == 0
        assert summary["invoices"]["errors"]
        assert db.query(Invoice).count() == 0
        assert db.query(Contact).count() == 0

    _reset_settings_and_db()


def test_fakturek_xml_v1_restores_credit_note_source_and_limit(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = _fakturek_credit_pair_payload()
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-credit-pair.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import ImportRun, Invoice

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["errors"] == []
        assert preview["invoices"]["will_import"] == 2
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        assert summary["invoices"]["imported"] == 2
        original = db.query(Invoice).filter_by(number="2026-ORIGINAL-77").one()
        credit = db.query(Invoice).filter_by(number="2026-CREDIT-78").one()
        assert credit.source_invoice_id == original.id
        assert credit.total_cents == -6050

    _reset_settings_and_db()


def test_fakturek_xml_v2_restores_credit_dependency_and_its_payment(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-credit-pair-v2.xml",
        payload=_fakturek_credit_pair_v2_payload(),
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import ImportRun, Invoice, Payment

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["errors"] == []
        assert preview["invoices"]["will_import"] == 2
        assert preview["payments"]["will_create"] == 1

        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()
        original = db.query(Invoice).filter_by(number="2026-ORIGINAL-77").one()
        credit = db.query(Invoice).filter_by(number="2026-CREDIT-78").one()
        payment = db.query(Payment).one()
        assert summary["invoices"]["imported"] == 2
        assert summary["payments"]["created"] == 1
        assert credit.source_invoice_id == original.id
        assert payment.invoice_id == original.id
        assert original.status == "paid"
        assert original.paid_on is None

    _reset_settings_and_db()


def test_fakturek_xml_v1_over_credit_rolls_back_whole_native_document(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = _fakturek_credit_pair_payload(over_credit=True)
    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-over-credit.xml",
        payload=payload,
        source="invoice_xml",
    )

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import Contact, ImportRun, Invoice

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["will_import"] == 0
        assert preview["invoices"]["errors"]
        with pytest.raises(ValueError, match="překračuje"):
            process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.rollback()
        assert db.query(Invoice).count() == 0
        assert db.query(Contact).count() == 0

    _reset_settings_and_db()


def test_fakturek_xml_credit_does_not_attach_to_unrelated_number_collision(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.fakturoid_import import preview_import_run, process_import_run
    from fakturek.models import Contact, ImportRun, Invoice

    with SessionLocal() as db:
        contact = Contact(subject_id=1, name="Unrelated local buyer")
        db.add(contact)
        db.flush()
        db.add(
            Invoice(
                subject_id=1,
                contact_id=int(contact.id),
                number="2026-ORIGINAL-77",
                document_type="invoice",
                status="paid",
                issue_date=date(2026, 1, 1),
                due_date=date(2026, 1, 15),
                currency="CZK",
                total_cents=999_999,
            )
        )
        db.commit()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="fakturek-collision.xml",
        payload=_fakturek_credit_pair_payload(),
        source="invoice_xml",
    )
    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        preview = preview_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        assert preview["invoices"]["will_import"] == 0
        assert any("nebude" in row["error"] for row in preview["invoices"]["errors"])
        with pytest.raises(ValueError, match="nebyla"):
            process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.rollback()
        assert db.query(Invoice).count() == 1
        assert db.query(Invoice).one().total_cents == 999_999

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

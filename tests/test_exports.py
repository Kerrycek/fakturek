from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import io
import re
import zipfile
import xml.etree.ElementTree as ET

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.public_links import build_public_invoice_urls
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "exports.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import (
        AuditLog,
        Contact,
        Invoice,
        InvoiceEmail,
        InvoiceItem,
        InvoiceSeries,
        Payment,
        Subject,
        SubjectBankAccount,
    )

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                public_username="acme-test",
                name="ACME Test s.r.o.",
                email="owner@example.test",
                street="Hlavní 1",
                city="Praha",
                zip="11000",
                country="CZ",
                ico="12345678",
                dic="CZ12345678",
                is_vat_payer=True,
                default_currency="CZK",
            )
        )
        db.add_all(
            [
                Contact(
                    id=1,
                    subject_id=1,
                    name="Jiří Chvojka",
                    email="jiri@example.test",
                    phone="+420123456789",
                    street="Dlouhá 1",
                    city="Praha",
                    zip="11000",
                    country="CZ",
                    ico="87654321",
                    dic="CZ87654321",
                    external_source="fakturoid",
                    external_id="contact-1",
                ),
                Contact(
                    id=2,
                    subject_id=1,
                    name="Anna Example",
                    email="anna@example.test",
                    city="Brno",
                    country="CZ",
                ),
            ]
        )
        db.add(
            InvoiceSeries(
                id=1,
                subject_id=1,
                name="default",
                prefix="2026-",
                pad_length=4,
                last_counter=2,
                last_counter_year=2026,
            )
        )
        db.add(
            SubjectBankAccount(
                id=1,
                subject_id=1,
                label="Hlavní účet",
                account_number="123456789/0100",
                iban="CZ6508000000192000145399",
                bic="GIBACZPX",
                country="CZ",
                is_default=True,
                sort_order=1,
            )
        )
        db.add_all(
            [
                Invoice(
                    id=1,
                    subject_id=1,
                    number="2026-0001",
                    status="paid",
                    issue_date=date(2026, 3, 1),
                    due_date=date(2026, 3, 15),
                    paid_on=date(2026, 3, 10),
                    currency="CZK",
                    notes="Roční příspěvek",
                    internal_notes="Interní poznámka",
                    contact_id=1,
                    buyer_name_cache="Jiří Chvojka",
                    rounding_adjustment_cents=0,
                    total_cents=12_100,
                    issued_at=datetime(2026, 3, 1, 10, 0, 0),
                    sent_at=datetime(2026, 3, 1, 10, 5, 0),
                    pdf_generated_at=datetime(2026, 3, 1, 10, 1, 0),
                    public_token="public-token-1",
                    series_id=1,
                    bank_account_id=1,
                    bank_account_label="Hlavní účet",
                    bank_account_number="123456789/0100",
                    bank_account_iban="CZ6508000000192000145399",
                    bank_account_bic="GIBACZPX",
                    bank_account_country="CZ",
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    number="DRAFT-2",
                    status="draft",
                    issue_date=date(2026, 3, 2),
                    due_date=date(2026, 3, 16),
                    currency="CZK",
                    notes="Koncept",
                    contact_id=2,
                    buyer_name_cache="Anna Example",
                    rounding_adjustment_cents=0,
                    total_cents=2_420,
                    series_id=1,
                ),
                Invoice(
                    id=3,
                    subject_id=1,
                    number="2026-0002",
                    status="sent",
                    issue_date=date(2026, 3, 3),
                    due_date=date(2026, 3, 17),
                    currency="CZK",
                    notes="Servisní práce",
                    contact_id=2,
                    buyer_name_cache="Anna Example",
                    rounding_adjustment_cents=0,
                    total_cents=30000,
                    issued_at=datetime(2026, 3, 3, 9, 0, 0),
                    sent_at=datetime(2026, 3, 3, 9, 5, 0),
                    public_token="public-token-3",
                    series_id=1,
                ),
            ]
        )
        db.add_all(
            [
                InvoiceItem(
                    invoice_id=1,
                    description="Členský příspěvek",
                    quantity=Decimal("1.00"),
                    unit_price_cents=10_000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=10_000,
                    line_vat_cents=2_100,
                    line_total_cents=12_100,
                    sort_order=1,
                ),
                InvoiceItem(
                    invoice_id=2,
                    description="Návrh služby",
                    quantity=Decimal("2.00"),
                    unit_price_cents=1_000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=2_000,
                    line_vat_cents=420,
                    line_total_cents=2_420,
                    sort_order=1,
                ),
            ]
        )
        db.add(
            Payment(
                id=1,
                invoice_id=1,
                paid_on=date(2026, 3, 10),
                amount_cents=12_100,
                note="Bankovní převod",
            )
        )
        db.add(
            InvoiceEmail(
                id=1,
                invoice_id=1,
                kind="invoice",
                from_email="owner@example.test",
                to_email="jiri@example.test",
                subject="Faktura 2026-0001",
                body="V příloze posílám fakturu.",
                status="sent",
                sent_at=datetime(2026, 3, 1, 10, 6, 0),
                message_id="msg-1",
            )
        )
        db.add(
            AuditLog(
                id=1,
                subject_id=1,
                user_id=1,
                action="invoice_created",
                entity_type="invoice",
                entity_id=1,
                data_json='{"number":"2026-0001"}',
                ip="127.0.0.1",
                user_agent="pytest",
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def test_contacts_export_csv(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/contacts/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment;" in response.headers["content-disposition"]

    text = response.content.decode("utf-8-sig")
    assert "name;email;phone" in text
    assert "Jiří Chvojka" in text
    assert "Anna Example" in text

    page = client.get("/imports")
    assert page.status_code == 200
    assert "/contacts/export.csv" in page.text

    _reset_settings_and_db()


def test_contacts_export_csv_escapes_spreadsheet_formulas(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import Contact

        contact = db.get(Contact, 2)
        assert contact is not None
        contact.name = "=HYPERLINK(\"https://evil.example\",\"x\")"
        contact.email = "+formula@example.test"
        db.commit()

    response = client.get("/contacts/export.csv")
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "'=HYPERLINK" in text
    assert "'+formula@example.test" in text

    _reset_settings_and_db()


def test_invoices_export_csv_respects_filters(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices/export.csv?status=paid")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]

    text = response.content.decode("utf-8-sig")
    assert "2026-0001" in text
    assert "2026-0002" not in text
    assert "DRAFT-2" not in text
    assert "Jiří Chvojka" in text

    page = client.get("/imports")
    assert page.status_code == 200
    assert 'action="/exports/invoices"' in page.text
    assert "Export faktur na míru" in page.text

    _reset_settings_and_db()


def test_invoices_export_csv_includes_sent_rows_when_filters_match(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices/export.csv?status=sent")
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "2026-0002" in text
    assert "Servisní práce" in text

    _reset_settings_and_db()


def test_custom_invoice_export_csv_supports_unicode_subject_filename(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Subject

    with SessionLocal() as db:
        subject = db.get(Subject, 1)
        assert subject is not None
        subject.name = "Česká účetní s.r.o."
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={
            "status": "paid",
            "format": "csv",
        },
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert 'filename="Ceska-ucetni-s-r-o' in disposition
    assert "filename*=UTF-8''%C4%8Cesk%C3%A1-%C3%BA%C4%8Detn%C3%AD-s-r-o" in disposition
    assert "2026-0001" in response.content.decode("utf-8-sig")

    _reset_settings_and_db()


def test_invoices_list_shows_newest_issue_date_first(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/invoices")
    assert response.status_code == 200

    billing_pos = response.text.index("2026-0002")
    draft_pos = response.text.index("DRAFT-2")
    paid_pos = response.text.index("2026-0001")
    assert billing_pos < draft_pos < paid_pos
    _reset_settings_and_db()


def test_full_export_zip_contains_expected_files(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/exports/data.zip")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
        assert {
            "README.txt",
            "subject.csv",
            "contacts.csv",
            "invoices.csv",
            "invoice_items.csv",
            "bank_accounts.csv",
            "payments.csv",
            "invoice_emails.csv",
            "audit_log.csv",
        }.issubset(names)

        readme = zf.read("README.txt").decode("utf-8")
        assert "Fakturek – kompletní export dat" in readme
        assert "contacts.csv" in readme

        contacts_csv = zf.read("contacts.csv").decode("utf-8-sig")
        invoices_csv = zf.read("invoices.csv").decode("utf-8-sig")
        items_csv = zf.read("invoice_items.csv").decode("utf-8-sig")
        audit_csv = zf.read("audit_log.csv").decode("utf-8-sig")

        assert "Jiří Chvojka" in contacts_csv
        assert "2026-0001" in invoices_csv
        assert "Členský příspěvek" in items_csv
        assert "invoice_created" in audit_csv

    imports_page = client.get("/imports")
    assert imports_page.status_code == 200
    assert "Kompletní ZIP" in imports_page.text
    assert "/exports/data.zip" in imports_page.text

    _reset_settings_and_db()


def test_imports_page_shows_advanced_invoice_export_builder(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.get("/imports")

    assert response.status_code == 200
    assert "Export faktur na míru" in response.text
    assert 'action="/exports/invoices"' in response.text
    assert 'name="contact_ids"' in response.text
    assert 'name="format"' in response.text
    assert "Jeden sloučený PDF" in response.text

    _reset_settings_and_db()


def test_import_upload_accepts_multipart_with_csrf(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = b"<contacts></contacts>"

    page = client.get("/imports")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    csrf_token = match.group(1)

    response = client.post(
        "/imports",
        data={
            "source": "fakturoid",
            "csrf_token": csrf_token,
        },
        files={
            "file": ("contacts.xml", payload, "application/xml"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/imports/")

    from fakturek.models import ImportRun

    with SessionLocal() as db:
        runs = db.query(ImportRun).all()
        assert len(runs) == 1
        assert runs[0].file_name == "contacts.xml"

    _reset_settings_and_db()


def test_import_upload_handles_cross_device_temp_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    payload = b"name,email\nCross-device customer,cross-device@example.test\n"

    page = client.get("/imports")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None

    import errno
    import fakturek.main as main_module

    original_replace = main_module.os.replace
    calls = 0

    def replace_with_cross_device_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_replace(source, destination)

    monkeypatch.setattr(main_module.os, "replace", replace_with_cross_device_once)
    response = client.post(
        "/imports",
        data={"source": "contacts_csv", "csrf_token": match.group(1)},
        files={"file": ("contacts.csv", payload, "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == 2

    from fakturek.models import ImportRun

    with SessionLocal() as db:
        run = db.query(ImportRun).one()
        stored = tmp_path / "imports" / str(run.file_path)
        assert stored.read_bytes() == payload

    _reset_settings_and_db()


def test_custom_invoice_export_xml_respects_filters(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/exports/invoices",
        data={
            "q": "",
            "date_from": "2026-03-01",
            "date_to": "2026-03-01",
            "status": "paid",
            "document_type": "invoice",
            "contact_ids": ["1"],
            "format": "xml",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    root = ET.fromstring(response.content)
    invoices = root.findall("./invoices/invoice")
    assert len(invoices) == 1
    assert invoices[0].attrib["number"] == "2026-0001"
    assert invoices[0].findtext("contact/name") == "Jiří Chvojka"
    assert invoices[0].findtext("./items/item/description") == "Členský příspěvek"

    _reset_settings_and_db()


def test_custom_invoice_export_pohoda_xml_respects_filters(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/exports/invoices",
        data={
            "status": "paid",
            "document_type": "invoice",
            "format": "pohoda_xml",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "pohoda" in response.headers["content-disposition"].lower()

    root = ET.fromstring(response.content)
    ns = {
        "dat": "http://www.stormware.cz/schema/version_2/data.xsd",
        "inv": "http://www.stormware.cz/schema/version_2/invoice.xsd",
        "typ": "http://www.stormware.cz/schema/version_2/type.xsd",
    }
    invoice = root.find("./dat:dataPackItem/inv:invoice", ns)
    assert invoice is not None
    assert invoice.findtext("./inv:invoiceHeader/inv:symVar", namespaces=ns) == "2026-0001"
    assert invoice.findtext("./inv:invoiceHeader/inv:partnerIdentity/typ:address/typ:company", namespaces=ns) == "Jiří Chvojka"
    assert invoice.findtext("./inv:invoiceDetail/inv:invoiceItem/inv:text", namespaces=ns) == "Členský příspěvek"

    _reset_settings_and_db()


def test_custom_invoice_export_money_s3_xml_respects_filters(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/exports/invoices",
        data={
            "status": "paid",
            "document_type": "invoice",
            "format": "money_s3_xml",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "money-s3" in response.headers["content-disposition"].lower()

    root = ET.fromstring(response.content)
    invoice = root.find("./SeznamFaktVyd/FaktVyd")
    assert invoice is not None
    assert invoice.findtext("Doklad") == "2026-0001"
    assert invoice.findtext("VarSymbol") == "2026-0001"
    assert invoice.findtext("./Partner/Nazev") == "Jiří Chvojka"
    assert invoice.findtext("./Polozky/Polozka/Nazev") == "Členský příspěvek"

    _reset_settings_and_db()


def test_custom_invoice_export_csv_bundle_contains_filtered_rows(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/exports/invoices",
        data={
            "status": "paid",
            "format": "csv_bundle",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert {"README.txt", "invoices.csv", "invoice_items.csv"}.issubset(set(zf.namelist()))
        invoices_csv = zf.read("invoices.csv").decode("utf-8-sig")
        items_csv = zf.read("invoice_items.csv").decode("utf-8-sig")
        assert "2026-0001" in invoices_csv
        assert "DRAFT-2" not in invoices_csv
        assert "Členský příspěvek" in items_csv
        assert "Návrh služby" not in items_csv

    _reset_settings_and_db()


def test_custom_invoice_export_pdf_zip_contains_selected_invoice_pdf(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/exports/invoices",
        data={
            "status": "paid",
            "contact_ids": ["1"],
            "format": "pdf_zip",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
        assert "2026-0001.pdf" in names
        assert "DRAFT-2.pdf" not in names
        assert zf.read("2026-0001.pdf").startswith(b"%PDF")

    _reset_settings_and_db()


def test_public_invoice_preview_offers_isdoc_download(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    urls = build_public_invoice_urls(
        public_username="acme-test",
        token="public-token-1",
        invoice_number="2026-0001",
        invoice_id=1,
        secret_key="test-secret",
    )

    preview = client.get(urls["view"])
    assert preview.status_code == 200
    assert urls["isdoc_download"] in preview.text

    isdoc = client.get(urls["isdoc_download"])
    assert isdoc.status_code == 200
    assert isdoc.headers["content-type"].startswith("application/xml")
    assert "attachment;" in isdoc.headers["content-disposition"]

    root = ET.fromstring(isdoc.content)
    ns = {"isdoc": "http://isdoc.cz/namespace/2013"}
    assert root.tag.endswith("Invoice")
    assert root.attrib["version"] == "6.0.2"
    assert root.findtext("./isdoc:DocumentType", namespaces=ns) == "1"
    assert root.findtext("./isdoc:ID", namespaces=ns) == "2026-0001"
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", root.findtext("./isdoc:UUID", namespaces=ns) or "")
    assert root.find("./isdoc:DueDate", namespaces=ns) is None
    assert root.find("./isdoc:AccountingSupplierParty/isdoc:Party", namespaces=ns) is not None
    assert root.find("./isdoc:AccountingCustomerParty/isdoc:Party", namespaces=ns) is not None
    assert root.findtext("./isdoc:PaymentMeans/isdoc:Payment/isdoc:Details/isdoc:VariableSymbol", namespaces=ns) == "20260001"
    invoice_lines = root.findall("./isdoc:InvoiceLines/isdoc:InvoiceLine", namespaces=ns)
    assert len(invoice_lines) == 1
    assert invoice_lines[0].findtext("./isdoc:Item/isdoc:Description", namespaces=ns) == "Členský příspěvek"

    _reset_settings_and_db()

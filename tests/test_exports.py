from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import io
from pathlib import Path
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


EXPECTED_IMPORT_SOURCES = frozenset(
    {
        "fakturek_contacts_csv_v1",
        "fakturek_catalog_csv_v1",
        "fakturek_native_v1",
        "fakturek_native_v2",
        "fakturek_native_v3",
        "fakturoid",
        "pohoda_xml",
        "money_s3_xml",
        "contacts_csv",
        "isdoc",
        "invoice_xml",
        "pdf_archive",
    }
)


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
    assert "Fakturek XML v1" in response.text
    assert "Fakturek XML v2 (včetně plateb)" in response.text
    assert "Fakturek XML / jiné XML" in response.text
    assert "Znovu importuje Fakturek XML v1 i starší exporty" in response.text

    _reset_settings_and_db()


def test_import_history_uses_human_labels_with_safe_fallback(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import ImportRun

    with SessionLocal() as db:
        known = ImportRun(
            subject_id=1,
            source="pohoda_xml",
            status="finished",
            file_name="invoices.xml",
        )
        unknown = ImportRun(
            subject_id=1,
            source="legacy_custom",
            status="legacy_state",
            file_name="legacy.dat",
        )
        db.add_all(
            [
                known,
                unknown,
                ImportRun(subject_id=1, source="contacts_csv", status="uploaded"),
                ImportRun(subject_id=1, source="invoice_xml", status="running"),
                ImportRun(subject_id=1, source="pdf_archive", status="error"),
            ]
        )
        db.commit()
        known_id = int(known.id)

    response = client.get("/imports")
    assert response.status_code == 200
    assert "<td>POHODA XML</td>" in response.text
    assert "<td>Dokončeno</td>" in response.text
    assert "<td>Nahráno</td>" in response.text
    assert "<td>Probíhá</td>" in response.text
    assert "<td>Chyba</td>" in response.text
    assert "<td>legacy_custom</td>" in response.text
    assert "<td>legacy_state</td>" in response.text

    detail = client.get(f"/imports/{known_id}")
    assert detail.status_code == 200
    assert "<tr><th>Zdroj</th><td>POHODA XML</td></tr>" in detail.text
    assert "<tr><th>Stav</th><td>Dokončeno</td></tr>" in detail.text

    switched = client.post(
        "/settings/language",
        data={"ui_language": "en", "next": "/imports"},
        follow_redirects=False,
    )
    assert switched.status_code == 303

    english_response = client.get("/imports")
    assert english_response.status_code == 200
    assert '<html lang="en"' in english_response.text
    assert "<strong>Fakturek Catalog CSV v1</strong>" in english_response.text
    assert "<strong>PDF / ZIP archive</strong>" in english_response.text
    assert "Fakturek XML v1" in english_response.text
    assert "Fakturek XML v2 (including payments)" in english_response.text
    assert "<strong>Fakturek XML / other XML</strong>" in english_response.text
    assert "Re-imports Fakturek XML v1 and older exports" in english_response.text
    assert "<td>POHODA XML</td>" in english_response.text
    assert "<td>Completed</td>" in english_response.text
    assert "<td>Uploaded</td>" in english_response.text
    assert "<td>In progress</td>" in english_response.text
    assert "<td>Error</td>" in english_response.text
    assert "<td>legacy_custom</td>" in english_response.text
    assert "<td>legacy_state</td>" in english_response.text

    english_detail = client.get(f"/imports/{known_id}")
    assert english_detail.status_code == 200
    assert "<td>POHODA XML</td>" in english_detail.text
    assert "<td>Completed</td>" in english_detail.text

    _reset_settings_and_db()


@pytest.mark.parametrize(
    ("stored_source", "display_source"),
    [
        ("legacy_custom", "legacy_custom"),
        ("  PoHoDa_XmL  ", "POHODA XML"),
    ],
)
def test_unsupported_import_run_is_read_only_and_fail_closed(
    monkeypatch,
    tmp_path,
    stored_source,
    display_source,
):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    import fakturek.fakturoid_import as importer
    from fakturek.models import Contact, ImportMap, ImportRun, Invoice

    importer_calls = {"preview": 0, "process": 0}

    def unexpected_preview(*args, **kwargs):
        importer_calls["preview"] += 1
        raise AssertionError("unsupported source reached preview importer")

    def unexpected_process(*args, **kwargs):
        importer_calls["process"] += 1
        raise AssertionError("unsupported source reached process importer")

    monkeypatch.setattr(importer, "preview_import_run", unexpected_preview)
    monkeypatch.setattr(importer, "process_import_run", unexpected_process)

    original_summary = '{"marker":"unchanged"}'
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=stored_source,
            status="uploaded",
            file_name="legacy.dat",
            summary_json=original_summary,
        )
        db.add(run)
        db.commit()
        run_id = int(run.id)
        before_counts = (
            db.query(Contact).count(),
            db.query(Invoice).count(),
            db.query(ImportMap).count(),
        )

    blocked_message = (
        "Zdroj tohoto importu není podporovaný. Běh zůstává dostupný jen pro audit; "
        "nelze ho upravit ani spustit."
    )
    detail = client.get(f"/imports/{run_id}")
    assert detail.status_code == 200
    assert display_source in detail.text
    assert blocked_message in detail.text
    assert f'action="/imports/{run_id}/process"' not in detail.text
    assert f'action="/imports/{run_id}/config"' not in detail.text
    assert importer_calls == {"preview": 0, "process": 0}

    config_response = client.post(
        f"/imports/{run_id}/config",
        data={"contact_conflict_mode": "create_new"},
        follow_redirects=False,
    )
    assert config_response.status_code == 409
    assert config_response.json() == {"detail": blocked_message}

    process_response = client.post(
        f"/imports/{run_id}/process",
        follow_redirects=False,
    )
    assert process_response.status_code == 409
    assert process_response.json() == {"detail": blocked_message}
    assert importer_calls == {"preview": 0, "process": 0}

    with SessionLocal() as db:
        unchanged = db.get(ImportRun, run_id)
        assert unchanged is not None
        assert unchanged.source == stored_source
        assert unchanged.status == "uploaded"
        assert unchanged.summary_json == original_summary
        assert (
            db.query(Contact).count(),
            db.query(Invoice).count(),
            db.query(ImportMap).count(),
        ) == before_counts

    switched = client.post(
        "/settings/language",
        data={"ui_language": "en", "next": f"/imports/{run_id}"},
        follow_redirects=False,
    )
    assert switched.status_code == 303

    english_message = (
        "This import source is not supported. The run remains available for audit only; "
        "it cannot be changed or started."
    )
    english_detail = client.get(f"/imports/{run_id}")
    assert english_detail.status_code == 200
    assert english_message in english_detail.text
    english_process = client.post(
        f"/imports/{run_id}/process",
        follow_redirects=False,
    )
    assert english_process.status_code == 409
    assert english_process.json() == {"detail": english_message}
    assert importer_calls == {"preview": 0, "process": 0}
    with SessionLocal() as db:
        final_run = db.get(ImportRun, run_id)
        assert final_run is not None
        assert (
            final_run.source,
            final_run.status,
            final_run.summary_json,
        ) == (stored_source, "uploaded", original_summary)
        assert (
            db.query(Contact).count(),
            db.query(Invoice).count(),
            db.query(ImportMap).count(),
        ) == before_counts

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


def test_import_upload_accepts_every_rendered_source(monkeypatch, tmp_path):
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    page = client.get("/imports")
    assert page.status_code == 200
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf_match is not None
    sources = re.findall(r'name="source"\s+value="([^"]+)"', page.text)
    assert len(sources) == 12
    assert set(sources) == EXPECTED_IMPORT_SOURCES

    for source in sources:
        response = client.post(
            "/imports",
            data={"source": source, "csrf_token": csrf_match.group(1)},
            files={"file": (f"{source}.xml", source.encode(), "application/xml")},
            follow_redirects=False,
        )
        assert response.status_code == 303, source

    normalized_response = client.post(
        "/imports",
        data={
            "source": "  PoHoDa_XmL  ",
            "csrf_token": csrf_match.group(1),
        },
        files={"file": ("normalized.xml", b"normalized", "application/xml")},
        follow_redirects=False,
    )
    assert normalized_response.status_code == 303

    for fallback_name, source_data in (
        ("missing", {}),
        ("blank", {"source": " \t "}),
    ):
        response = client.post(
            "/imports",
            data={"csrf_token": csrf_match.group(1), **source_data},
            files={
                "file": (
                    f"{fallback_name}.xml",
                    fallback_name.encode(),
                    "application/xml",
                )
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, fallback_name

    from fakturek.models import ImportRun

    with SessionLocal() as db:
        runs = db.query(ImportRun).all()
        assert {run.source for run in runs} == EXPECTED_IMPORT_SOURCES
        assert len(runs) == len(EXPECTED_IMPORT_SOURCES) + 3
        assert sum(run.source == "fakturoid" for run in runs) == 3
        assert sum(run.source == "pohoda_xml" for run in runs) == 2

    _reset_settings_and_db()


def test_import_upload_rejects_unknown_source_before_persisting(monkeypatch, tmp_path):
    import_root = tmp_path / "imports"
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(import_root))
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    page = client.get("/imports")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None

    response = client.post(
        "/imports",
        data={
            "source": "totally_unknown",
            "csrf_token": match.group(1),
        },
        files={
            "file": ("unknown.xml", b"<contacts></contacts>", "application/xml"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Neplatný zdroj importu. Vyber jeden z nabízených typů." in response.text

    from fakturek.models import ImportRun

    with SessionLocal() as db:
        assert db.query(ImportRun).count() == 0
    assert not import_root.exists() or not any(import_root.rglob("*"))

    switched = client.post(
        "/settings/language",
        data={"ui_language": "en", "next": "/imports"},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    english_response = client.post(
        "/imports",
        data={
            "source": "totally_unknown",
            "csrf_token": match.group(1),
        },
        files={
            "file": ("unknown.xml", b"<contacts></contacts>", "application/xml"),
        },
        follow_redirects=False,
    )
    assert english_response.status_code == 400
    assert (
        "Invalid import source. Choose one of the available types."
        in english_response.text
    )
    with SessionLocal() as db:
        assert db.query(ImportRun).count() == 0
    assert not import_root.exists() or not any(import_root.rglob("*"))

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
    assert root.attrib["format"] == "fakturek_invoice_export"
    assert root.attrib["version"] == "1"
    assert root.attrib["origin_subject_id"] == "1"
    assert "subject_id" not in root.attrib
    invoices = root.findall("./invoices/invoice")
    assert len(invoices) == 1
    assert invoices[0].attrib["number"] == "2026-0001"
    assert invoices[0].attrib["status"] == "paid"
    assert invoices[0].findtext("taxable_supply_date") == "2026-03-01"
    assert invoices[0].findtext("sent_at") == "2026-03-01T10:05:00"
    assert invoices[0].findtext("contact/name") == "Jiří Chvojka"
    assert invoices[0].findtext("contact/phone") == "+420123456789"
    assert invoices[0].findtext("contact/dic") == "CZ87654321"
    assert invoices[0].findtext("bank_account/label") == "Hlavní účet"
    assert invoices[0].findtext("bank_account/iban") == "CZ6508000000192000145399"
    assert invoices[0].findtext("./items/item/description") == "Členský příspěvek"
    assert invoices[0].find("./payments") is None

    from fakturek.fakturoid_import import parse_fakturek_invoices_xml_v1

    parsed = parse_fakturek_invoices_xml_v1(response.content)
    assert len(parsed) == 1
    assert parsed[0].number == "2026-0001"
    assert parsed[0].buyer.name == "Jiří Chvojka"
    assert parsed[0].total_cents == 12_100

    _reset_settings_and_db()


def test_fakturek_xml_v2_export_preserves_ordered_signed_payments(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Payment

    with SessionLocal() as db:
        db.add(
            Payment(
                id=2,
                invoice_id=1,
                paid_on=date(2026, 3, 11),
                amount_cents=-100,
                note="Korekce platby",
            )
        )
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={
            "date_from": "2026-03-01",
            "date_to": "2026-03-01",
            "status": "paid",
            "document_type": "invoice",
            "contact_ids": ["1"],
            "format": "xml_v2",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "-v2.xml" in response.headers["content-disposition"]
    root = ET.fromstring(response.content)
    assert root.attrib["version"] == "2"
    assert root.attrib["origin_subject_id"] == "1"
    payments = root.findall("./invoices/invoice/payments/payment")
    assert [payment.attrib["id"] for payment in payments] == ["1", "2"]
    assert [payment.findtext("paid_on") for payment in payments] == [
        "2026-03-10",
        "2026-03-11",
    ]
    assert [payment.findtext("amount_cents") for payment in payments] == ["12100", "-100"]
    assert [payment.findtext("note") for payment in payments] == [
        "Bankovní převod",
        "Korekce platby",
    ]

    from fakturek.fakturoid_import import parse_fakturek_invoices_xml_v2

    parsed = parse_fakturek_invoices_xml_v2(response.content)
    assert [payment.amount_cents for payment in parsed[0].payments] == [12_100, -100]
    assert [payment.note for payment in parsed[0].payments] == [
        "Bankovní převod",
        "Korekce platby",
    ]

    _reset_settings_and_db()


@pytest.mark.parametrize(
    "unsafe_note",
    ["Neplatný\x01text", "Změněný\rřádek", "C1\x85text", "X\ufdd0Y"],
)
def test_fakturek_xml_v2_export_rejects_non_roundtrip_payment_note(
    monkeypatch,
    tmp_path,
    unsafe_note,
):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Payment

    with SessionLocal() as db:
        payment = db.get(Payment, 1)
        assert payment is not None
        payment.note = unsafe_note
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={"status": "paid", "format": "xml_v2"},
    )

    assert response.status_code == 422
    assert "Poznámka platby obsahuje nepovolené znaky XML" in response.text

    _reset_settings_and_db()


@pytest.mark.parametrize(
    ("constant_name", "limit", "error_text"),
    [
        ("FAKTUREK_INVOICE_XML_MAX_INVOICES", 2, "příliš mnoho faktur"),
        ("FAKTUREK_INVOICE_XML_MAX_ITEMS", 1, "příliš mnoho položek"),
        ("FAKTUREK_INVOICE_XML_MAX_PAYMENTS", 0, "příliš mnoho plateb"),
    ],
)
def test_fakturek_xml_v2_export_enforces_importer_row_limits(
    monkeypatch,
    tmp_path,
    constant_name,
    limit,
    error_text,
):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    import fakturek.fakturoid_import as import_module

    monkeypatch.setattr(import_module, constant_name, limit)
    response = client.post("/exports/invoices", data={"format": "xml_v2"})

    assert response.status_code == 422
    assert error_text in response.text

    _reset_settings_and_db()


def test_fakturek_xml_v2_export_stays_within_current_import_upload_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("IMPORT_MAX_UPLOAD_MB", "1")
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        original = db.get(Invoice, 1)
        assert original is not None
        original.notes = "N" * 100_000
        original.internal_notes = "I" * 100_000
        for invoice_id in range(4, 9):
            db.add(
                Invoice(
                    id=invoice_id,
                    subject_id=1,
                    contact_id=1,
                    number=f"2026-SIZE-{invoice_id}",
                    status="paid",
                    issue_date=date(2026, 3, invoice_id),
                    due_date=date(2026, 3, invoice_id + 10),
                    paid_on=date(2026, 3, invoice_id + 1),
                    currency="CZK",
                    notes="N" * 100_000,
                    internal_notes="I" * 100_000,
                    total_cents=12_100,
                    discount_cents=0,
                    rounding_adjustment_cents=0,
                )
            )
            db.add(
                InvoiceItem(
                    invoice_id=invoice_id,
                    description="Položka",
                    quantity=Decimal("1.00"),
                    unit_price_cents=10_000,
                    vat_rate=Decimal("21.00"),
                    line_net_cents=10_000,
                    line_vat_cents=2_100,
                    line_total_cents=12_100,
                    sort_order=0,
                )
            )
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={"status": "paid", "format": "xml_v2"},
    )

    assert response.status_code == 422
    assert "překračuje maximální velikost povolenou pro import" in response.text

    _reset_settings_and_db()


def test_fakturek_xml_v1_export_round_trips_draft_zero_vat(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        invoice = db.get(Invoice, 2)
        item = db.query(InvoiceItem).filter_by(invoice_id=2).one()
        invoice.total_cents = 2_000
        item.vat_rate = Decimal("0.00")
        item.line_net_cents = 2_000
        item.line_vat_cents = 0
        item.line_total_cents = 2_000
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={"status": "draft", "format": "xml"},
    )
    assert response.status_code == 200

    from fakturek.fakturoid_import import parse_fakturek_invoices_xml_v1

    parsed = parse_fakturek_invoices_xml_v1(response.content)
    assert len(parsed) == 1
    assert parsed[0].status == "draft"
    assert parsed[0].lines[0].vat_rate == Decimal("0.00")
    assert parsed[0].lines[0].vat_cents == 0
    assert parsed[0].total_cents == 2_000

    _reset_settings_and_db()


def test_fakturek_xml_v1_export_preserves_credit_note_source_identity(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice, InvoiceItem

    with SessionLocal() as db:
        db.add(
            Invoice(
                id=4,
                subject_id=1,
                contact_id=1,
                number="2026-CREDIT-1",
                status="issued",
                document_type="credit_note",
                source_invoice_id=1,
                issue_date=date(2026, 3, 20),
                due_date=date(2026, 3, 20),
                currency="CZK",
                total_cents=-6_050,
                discount_cents=0,
                rounding_adjustment_cents=0,
            )
        )
        db.add(
            InvoiceItem(
                invoice_id=4,
                description="Částečný dobropis",
                quantity=Decimal("1.00"),
                unit_price_cents=-5_000,
                vat_rate=Decimal("21.00"),
                line_net_cents=-5_000,
                line_vat_cents=-1_050,
                line_total_cents=-6_050,
                sort_order=1,
            )
        )
        db.commit()

    response = client.post(
        "/exports/invoices",
        data={"document_type": "credit_note", "format": "xml"},
    )
    assert response.status_code == 200
    root = ET.fromstring(response.content)
    invoices_xml = root.findall("./invoices/invoice")
    assert len(invoices_xml) == 2
    assert root.find("./invoices").attrib == {
        "count": "2",
        "selected_count": "1",
        "dependency_count": "1",
    }
    credit_xml = next(invoice for invoice in invoices_xml if invoice.attrib["document_type"] == "credit_note")
    assert credit_xml.findtext("source_invoice_id") == "1"
    assert credit_xml.findtext("source_invoice_number") == "2026-0001"

    from fakturek.fakturoid_import import parse_fakturek_invoices_xml_v1, process_import_run
    from fakturek.models import ImportRun, Subject

    parsed = parse_fakturek_invoices_xml_v1(response.content)
    assert len(parsed) == 2
    parsed_credit = next(invoice for invoice in parsed if invoice.document_type == "credit_note")
    assert parsed_credit.source_invoice_external_id == "v1:1:invoice:1"
    assert parsed_credit.total_cents == -6_050

    import_root = Path(tmp_path) / "roundtrip-imports"
    payload_sha256 = hashlib.sha256(response.content).hexdigest()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=2,
                public_username="roundtrip-target",
                name="Round-trip target",
                email="target@example.test",
                country="CZ",
                default_currency="CZK",
            )
        )
        run = ImportRun(
            subject_id=2,
            source="invoice_xml",
            status="uploaded",
            file_name="credit-note.xml",
            file_path="",
            file_sha256=payload_sha256,
            file_size_bytes=len(response.content),
            mime_type="application/xml",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        relative_path = Path(f"subject-2/run-{int(run.id)}/credit-note.xml")
        stored_path = import_root / relative_path
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(response.content)
        run.file_path = relative_path.as_posix()

        summary = process_import_run(db, run=run, subject_id=2, import_storage_root=import_root)
        db.commit()

        imported = db.query(Invoice).filter_by(subject_id=2).order_by(Invoice.id.asc()).all()
        assert summary["invoices"]["imported"] == 2
        assert len(imported) == 2
        imported_source = next(invoice for invoice in imported if invoice.document_type == "invoice")
        imported_credit = next(invoice for invoice in imported if invoice.document_type == "credit_note")
        assert imported_credit.source_invoice_id == imported_source.id
        assert imported_credit.total_cents == -6_050

    with SessionLocal() as db:
        source = db.get(Invoice, 1)
        assert source is not None
        source.status = "draft"
        db.commit()

    rejected = client.post(
        "/exports/invoices",
        data={"document_type": "credit_note", "format": "xml"},
    )
    assert rejected.status_code == 422
    assert "původní fakturou ve stavu konceptu" in rejected.text

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

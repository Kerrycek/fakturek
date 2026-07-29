from __future__ import annotations

from datetime import date
import hashlib
import io
from pathlib import Path
import zipfile

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("pypdf")
reportlab = pytest.importorskip("reportlab")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _build_invoice_pdf_bytes() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    lines = [
        "IČO IČO",
        "Bankovní účet",
        "Variabilní symbol",
        "Způsob platby Převodem",
        "Datum vystavení 05. 02. 2026",
        "Datum splatnosti 10. 02. 2026",
        "Faktura 2026-0002",
        "DODAVATEL ODBĚRATEL",
        "Jan Novák",
        "Testovací 1",
        "110 00 Praha",
        "12345678",
        "Neplátce DPH",
        "vpsFree.cz, z.s.",
        "Nad Dalejským údolím 2699/9",
        "155 00 Praha",
        "26568055",
        "5578244004/5500",
        "20260002",
        "CENA ZA MJ CELKEM",
        "50 hod Práce při správě a podpoře hostingové infrastruktury 500,00 Kč 25 000,00 Kč",
        "Fyzická osoba zapsaná v živnostenském rejstříku.",
    ]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 810
    for line in lines:
        c.drawString(36, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def _setup_sqlite_db(monkeypatch, tmp_path):
    db_path = tmp_path / "phase48.sqlite3"
    import_root = tmp_path / "imports"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(import_root))
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.models import Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Jan Novák",
                email="owner@example.test",
                public_username="jan-novak",
                street="Testovací 1",
                city="Praha",
                zip="110 00",
                country="CZ",
                ico="12345678",
            )
        )
        db.commit()

    return SessionLocal, import_root


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


def test_parse_invoice_pdf_bytes_basic():
    from fakturek.fakturoid_import import parse_fakturoid_invoice_pdf

    inv = parse_fakturoid_invoice_pdf(_build_invoice_pdf_bytes(), filename="2026-0002.pdf")
    assert inv.number == "2026-0002"
    assert inv.issue_date == date(2026, 2, 5)
    assert inv.due_date == date(2026, 2, 10)
    assert inv.buyer.name == "vpsFree.cz, z.s."
    assert inv.buyer.ico == "26568055"
    assert len(inv.lines) == 1
    assert inv.lines[0].description.startswith("Práce")
    assert "hostingové infrastruktury" in inv.lines[0].description
    assert inv.lines[0].description.endswith("(hod)")
    assert inv.total_cents == 2500000


def test_process_import_run_accepts_zip_with_invoice_pdfs(monkeypatch, tmp_path):
    SessionLocal, import_root = _setup_sqlite_db(monkeypatch, tmp_path)

    pdf_bytes = _build_invoice_pdf_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("kerrycze-invoices-2026/2026-0002.pdf", pdf_bytes)
    zip_bytes = buf.getvalue()

    run_id = _create_import_run(
        SessionLocal,
        import_root,
        filename="kerrycze-invoices-2026.zip",
        payload=zip_bytes,
        mime_type="application/zip",
    )

    from fakturek.fakturoid_import import process_import_run
    from fakturek.models import Contact, ImportRun, Invoice, InvoiceItem

    with SessionLocal() as db:
        run = db.get(ImportRun, run_id)
        assert run is not None
        summary = process_import_run(db, run=run, subject_id=1, import_storage_root=import_root)
        db.commit()

        invoice = db.query(Invoice).order_by(Invoice.id.asc()).first()
        contact = db.query(Contact).order_by(Contact.id.asc()).first()
        item = db.query(InvoiceItem).order_by(InvoiceItem.id.asc()).first()

        assert summary["detected"]["pdf_files"] == 1
        assert summary["invoices"]["parsed"] == 1
        assert summary["invoices"]["imported"] == 1
        assert invoice is not None
        assert invoice.number == "2026-0002"
        assert invoice.total_cents == 2500000
        assert contact is not None
        assert contact.name == "vpsFree.cz, z.s."
        assert contact.ico == "26568055"
        assert item is not None
        assert item.quantity == pytest.approx(50)
        assert item.unit_price_cents == 50000

    _reset_settings_and_db()

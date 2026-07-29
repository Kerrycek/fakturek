from __future__ import annotations

from pathlib import Path

from fakturek.pdf_store import (
    invoice_pdf_relpath,
    persist_pdf_bytes,
    read_pdf_bytes,
    safe_filename_base,
    sha256_hex,
)


def test_safe_filename_base_sanitizes():
    assert safe_filename_base("INV 2026/0001") == "INV-2026-0001"
    assert safe_filename_base("") == "invoice"
    assert safe_filename_base("!!!", fallback="x") == "x"


def test_invoice_pdf_relpath_is_relative_and_stable():
    p = invoice_pdf_relpath(subject_id=1, invoice_id=42, invoice_number="INV 2026/0001")
    assert isinstance(p, Path)
    assert not p.is_absolute()
    assert ".." not in p.as_posix()
    assert p.as_posix().startswith("subject-1/")
    assert p.as_posix().endswith(".pdf")


def test_persist_and_read_pdf_bytes(tmp_path: Path):
    storage_root = tmp_path / "pdfs"
    pdf_bytes = b"%PDF-1.4\n%fake\n" + b"0" * 200

    relpath, digest = persist_pdf_bytes(
        storage_root=storage_root,
        subject_id=1,
        invoice_id=1,
        invoice_number="INV-0001",
        pdf_bytes=pdf_bytes,
    )

    assert relpath.startswith("subject-1/")
    assert digest == sha256_hex(pdf_bytes)

    loaded = read_pdf_bytes(storage_root, relpath)
    assert loaded == pdf_bytes


def test_read_pdf_bytes_rejects_path_traversal(tmp_path: Path):
    storage_root = tmp_path / "pdfs"
    storage_root.mkdir(parents=True, exist_ok=True)

    # Attempt to escape storage root should be rejected.
    assert read_pdf_bytes(storage_root, "../secret.pdf") is None

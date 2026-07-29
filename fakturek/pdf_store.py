from __future__ import annotations

"""Persisted PDF storage helpers.

Phase-20 introduces persisting *issued* invoice PDFs on disk (outside webroot)
and storing metadata on the invoice row:

- ``Invoice.pdf_path`` (relative path under storage root)
- ``Invoice.pdf_hash`` (sha256 hex)
- ``Invoice.pdf_generated_at`` (UTC datetime)

This module is intentionally framework-agnostic and does not import FastAPI or
SQLAlchemy.
"""

import hashlib
import os
import tempfile
from pathlib import Path


def resolve_storage_root(storage_dir: str | Path, *, project_root: Path) -> Path:
    """Resolve PDF storage root.

    - If ``storage_dir`` is relative, it is considered relative to ``project_root``.
    - Returned path is absolute and normalized.
    """

    raw = Path(str(storage_dir or "var/pdfs").strip() or "var/pdfs")
    root = raw if raw.is_absolute() else (project_root / raw)
    return root.resolve()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename_base(value: str, *, fallback: str = "invoice") -> str:
    """Return a filesystem-safe filename base (no extension)."""

    v = (value or "").strip()
    if not v:
        return fallback

    keep: list[str] = []
    for ch in v:
        if ch.isalnum() or ch in {"-", "_"}:
            keep.append(ch)
        else:
            keep.append("-")

    out = "".join(keep).strip("-")
    return out or fallback


def invoice_pdf_relpath(*, subject_id: int, invoice_id: int, invoice_number: str) -> Path:
    """Build a relative storage path for an invoice PDF.

    The returned path is **relative** (no leading slash).
    """

    safe_no = safe_filename_base(invoice_number, fallback=f"invoice-{invoice_id}")
    return Path(f"subject-{int(subject_id)}") / f"{int(invoice_id)}-{safe_no}.pdf"


def _safe_resolve_under_root(storage_root: Path, relpath: str | Path) -> Path:
    root = storage_root.resolve()
    p = Path(relpath)
    if p.is_absolute():
        raise ValueError("pdf_path must be relative")

    candidate = (root / p).resolve()
    # Prevent path traversal. Candidate must live inside root.
    if candidate == root or root in candidate.parents:
        return candidate
    raise ValueError("pdf_path escapes storage root")


def read_pdf_bytes(storage_root: Path, relpath: str | Path) -> bytes | None:
    """Read persisted PDF bytes.

    Returns ``None`` if the file is missing or the path is unsafe.
    """

    try:
        full_path = _safe_resolve_under_root(storage_root, relpath)
    except Exception:
        return None

    if not full_path.exists() or not full_path.is_file():
        return None

    try:
        return full_path.read_bytes()
    except Exception:
        return None


def persist_pdf_bytes(
    *,
    storage_root: Path,
    subject_id: int,
    invoice_id: int,
    invoice_number: str,
    pdf_bytes: bytes,
) -> tuple[str, str]:
    """Persist PDF bytes and return (relative_path, sha256_hex)."""

    digest = sha256_hex(pdf_bytes)
    relpath = invoice_pdf_relpath(
        subject_id=int(subject_id),
        invoice_id=int(invoice_id),
        invoice_number=str(invoice_number or ""),
    )

    full_path = _safe_resolve_under_root(storage_root, relpath)
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # Avoid rewriting identical content when possible.
    if full_path.exists() and full_path.is_file():
        try:
            if sha256_hex(full_path.read_bytes()) == digest:
                return relpath.as_posix(), digest
        except Exception:
            # Best-effort; if reading fails, proceed to rewrite.
            pass

    # Atomic write: write to a temp file in the same directory and replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(full_path.parent),
        prefix=f".{full_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, full_path)
    finally:
        # If anything failed before os.replace, ensure the temp file is removed.
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass

    return relpath.as_posix(), digest

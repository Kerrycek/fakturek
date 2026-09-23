from __future__ import annotations

import csv
import io

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module  # noqa: E402
from fakturek.db import Base  # noqa: E402
from fakturek.settings import get_settings  # noqa: E402


def _reset() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'contacts.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    _reset()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Subject

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all([Subject(id=1, name="One"), Subject(id=2, name="Two")])
        db.add_all(
            [
                Contact(
                    subject_id=1,
                    name="=Portable contact",
                    email="+portable@example.test",
                    phone="+420123456789",
                    street="Main 1",
                    city="Prague",
                    zip="11000",
                    country="CZ",
                    ico="12345678",
                    dic="CZ12345678",
                    fixed_variable_symbol="001234",
                    external_source="crm",
                    external_id="CRM-9",
                ),
                Contact(
                    subject_id=2,
                    name="Other tenant",
                    email="other@example.test",
                    fixed_variable_symbol="999",
                    external_source="crm",
                    external_id="FOREIGN-1",
                ),
            ]
        )
        db.commit()
    return TestClient(create_app())


def test_contacts_csv_v1_export_is_portable_tenant_scoped_and_reimportable(
    monkeypatch, tmp_path
):
    from fakturek.contacts_csv import HEADER, parse_contacts_csv_bytes

    client = _setup(monkeypatch, tmp_path)
    response = client.get("/exports/contacts.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "fakturek-contacts-v1.csv" in response.headers["content-disposition"]

    [row] = parse_contacts_csv_bytes(response.content)
    assert row.as_dict() == {
        "name": "=Portable contact",
        "email": "+portable@example.test",
        "phone": "+420123456789",
        "street": "Main 1",
        "city": "Prague",
        "zip": "11000",
        "country": "CZ",
        "ico": "12345678",
        "dic": "CZ12345678",
        "fixed_variable_symbol": "001234",
    }

    reader = csv.reader(
        io.StringIO(response.content.decode("utf-8-sig"), newline=""),
        delimiter=";",
        strict=True,
    )
    assert next(reader) == list(HEADER)
    assert "id" not in HEADER
    assert "external_id" not in HEADER
    assert "external_source" not in HEADER
    raw_row = next(reader)
    assert raw_row[1] == "'=Portable contact"
    assert raw_row[2] == "'+portable@example.test"
    assert list(reader) == []

    page = client.get("/imports")
    assert page.status_code == 200
    assert "/exports/contacts.csv" in page.text
    assert "Kontakty CSV v1" in page.text
    assert "Kontakty CSV (starší export)" in page.text

    legacy = client.get("/contacts/export.csv")
    assert legacy.status_code == 200
    legacy_header = next(
        csv.reader(io.StringIO(legacy.content.decode("utf-8-sig")), delimiter=";")
    )
    assert legacy_header[0] == "id"
    assert "external_id" in legacy_header
    _reset()


def test_contacts_csv_export_hard_clamps_large_config_to_25_mib(monkeypatch, tmp_path):
    monkeypatch.setenv("IMPORT_MAX_UPLOAD_MB", "100")
    client = _setup(monkeypatch, tmp_path)
    import fakturek.main as main_module

    observed: dict[str, int] = {}

    def too_large(*, contacts, max_rows, max_upload_bytes):
        observed["max_upload_bytes"] = max_upload_bytes
        raise ValueError("contacts CSV is too large")

    monkeypatch.setattr(main_module, "build_contacts_csv_bytes", too_large)
    response = client.get("/exports/contacts.csv")
    assert response.status_code == 413
    assert observed["max_upload_bytes"] == 25 * 1024 * 1024
    _reset()


def test_contacts_csv_import_ui_shows_the_effective_25_mib_limit(
    monkeypatch, tmp_path,
):
    from fakturek.contacts_csv import SOURCE

    monkeypatch.setenv("IMPORT_MAX_UPLOAD_MB", "100")
    client = _setup(monkeypatch, tmp_path)

    page = client.get("/imports")
    assert page.status_code == 200
    assert "data-max-upload-mb=\"25\"" in page.text
    assert "<span data-import-max-upload-mb>100</span>" in page.text
    assert "active.dataset.maxUploadMb" in page.text

    contacts_selected = client.post("/imports", data={"source": SOURCE})
    assert contacts_selected.status_code == 400
    assert "<span data-import-max-upload-mb>25</span>" in contacts_selected.text
    _reset()

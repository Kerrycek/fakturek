from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import zipfile
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup(monkeypatch, tmp_path, *, csrf: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'native.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "1" if csrf else "0")
    monkeypatch.setenv("SECRET_KEY", "native-test-secret")
    _reset()
    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, InvoiceCatalogItem, Subject

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all([Subject(id=1, name="Source"), Subject(id=2, name="Destination")])
        db.add(
            Contact(
                subject_id=1,
                name="Native customer",
                email="customer@example.test",
                phone="+420111222333",
                street="Export 1",
                city="Prague",
                zip="11000",
                country="CZ",
                ico="12345678",
                dic="CZ12345678",
                fixed_variable_symbol="42",
                registry_auto_update=False,
                external_source="crm",
                external_id="customer-7",
                registry_last_error="never export this",
                registry_data_hash="never export this either",
            )
        )
        db.add(
            InvoiceCatalogItem(
                subject_id=1,
                description="Native service",
                quantity=Decimal("2.00"),
                unit="hour",
                unit_price_cents=12345,
                vat_rate=Decimal("21.00"),
                currency="CZK",
            )
        )
        db.commit()
    return TestClient(create_app()), SessionLocal, tmp_path / "imports"


def _csrf(client: TestClient) -> str:
    page = client.get("/imports")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def _upload(client: TestClient, payload: bytes, *, csrf_token: str = "") -> int:
    response = client.post(
        "/imports",
        data={"source": "fakturek_native_v1", "csrf_token": csrf_token},
        files={
            "file": (
                "native-backup.zip",
                payload,
                "application/vnd.fakturek.native-backup+zip",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("/")[2].split("?")[0])


def _manifest_zip(*, contacts: bytes = b"", catalog: bytes = b"") -> bytes:
    datasets = [
        {
            "name": "contacts",
            "filename": "contacts.jsonl",
            "schema_version": 1,
            "row_count": contacts.count(b"\n"),
            "sha256": hashlib.sha256(contacts).hexdigest(),
        },
        {
            "name": "catalog_items",
            "filename": "catalog_items.jsonl",
            "schema_version": 1,
            "row_count": catalog.count(b"\n"),
            "sha256": hashlib.sha256(catalog).hexdigest(),
        },
    ]
    manifest = {
        "format": "fakturek-native-backup",
        "version": 1,
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "export_id": str(uuid4()),
        "datasets": datasets,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("contacts.jsonl", contacts)
        archive.writestr("catalog_items.jsonl", catalog)
    return out.getvalue()


def test_native_backup_export_and_round_trip_are_scoped_and_idempotent(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    response = client.get("/exports/native-backup.zip")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.fakturek.native-backup+zip")
    assert "attachment;" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["manifest.json", "contacts.jsonl", "catalog_items.jsonl"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "fakturek-native-backup"
        assert manifest["version"] == 1
        assert "source_subject_id" not in manifest
        assert manifest["export_id"]
        assert '"source_subject_id"' not in archive.read("manifest.json").decode("utf-8")
        for dataset in manifest["datasets"]:
            content = archive.read(dataset["filename"])
            assert dataset["row_count"] == content.count(b"\n")
            assert dataset["sha256"] == hashlib.sha256(content).hexdigest()
            assert dataset["schema_version"] == 1
        contact = json.loads(archive.read("contacts.jsonl").splitlines()[0])
        assert set(contact) <= {
            "name",
            "email",
            "phone",
            "street",
            "city",
            "zip",
            "country",
            "ico",
            "dic",
            "fixed_variable_symbol",
            "registry_auto_update",
            "external_source",
            "external_id",
        }
        assert "registry_last_error" not in contact and "registry_data_hash" not in contact

    run_id = _upload(client, response.content)
    detail = client.get(f"/imports/{run_id}")
    assert "Native backup v1" in detail.text
    assert "verified" in detail.text
    assert "Spustit import" in detail.text
    with SessionLocal() as db:
        from fakturek.models import ImportRun

        uploaded_run = db.get(ImportRun, run_id)
        assert uploaded_run is not None
        uploaded_run.status = "error"
        db.commit()
    assert "Spustit import" in client.get(f"/imports/{run_id}").text

    with SessionLocal() as db:
        from fakturek.models import ImportRun
        from fakturek.native_backup import process_native_backup_import

        run = ImportRun(
            subject_id=2,
            source="fakturek_native_v1",
            status="uploaded",
            file_name="native-backup.zip",
            file_sha256=hashlib.sha256(response.content).hexdigest(),
            file_size_bytes=len(response.content),
            mime_type="application/zip",
        )
        db.add(run)
        db.flush()
        stored = tmp_path / "imports" / f"subject-2/run-{run.id}.zip"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(response.content)
        run.file_path = stored.relative_to(tmp_path / "imports").as_posix()
        db.flush()
        process_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=tmp_path / "imports",
            max_upload_bytes=25 * 1024 * 1024,
        )
        process_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=tmp_path / "imports",
            max_upload_bytes=25 * 1024 * 1024,
        )
        db.commit()
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, InvoiceCatalogItem

        assert db.query(Contact).filter_by(subject_id=2).count() == 1
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=2).count() == 1
        assert db.query(Contact).filter_by(subject_id=1).count() == 1
        assert db.query(ImportMap).filter_by(subject_id=2, source="fakturek_native_v1").count() == 2
    _reset()


@pytest.mark.parametrize(
    "mutation", ["unknown", "duplicate", "traversal", "bad_hash", "bad_format", "bad_jsonl"]
)
def test_native_backup_rejects_unsafe_or_invalid_archives_before_processing(
    monkeypatch, tmp_path, mutation
):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    contacts = b'{"name":"New customer"}\n'
    payload = _manifest_zip(contacts=contacts)
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        members = {name: source.read(name) for name in source.namelist()}
    if mutation == "bad_jsonl":
        members["contacts.jsonl"] = b"{not json}\n"
    if mutation in {"bad_hash", "bad_format"}:
        manifest = json.loads(members["manifest.json"])
        if mutation == "bad_hash":
            manifest["datasets"][0]["sha256"] = "0" * 64
        else:
            manifest["format"] = "other"
        members["manifest.json"] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
        if mutation == "unknown":
            archive.writestr("surprise.txt", b"no")
        if mutation == "duplicate":
            archive.writestr("contacts.jsonl", contacts)
        if mutation == "traversal":
            archive.writestr("../contacts.jsonl", contacts)
    run_id = _upload(client, out.getvalue())
    detail = client.get(f"/imports/{run_id}")
    assert "Preview selhalo" in detail.text
    assert "Import nelze spustit" in detail.text
    assert "Spustit import" not in detail.text
    assert f'action="/imports/{run_id}/process"' not in detail.text
    processed = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert processed.status_code == 303
    assert processed.headers["location"] == f"/imports/{run_id}?error=1"
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportRun

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(Contact).filter_by(subject_id=1).count() == 1
    retry_detail = client.get(f"/imports/{run_id}")
    assert "Import nelze spustit" in retry_detail.text
    assert "Spustit import" not in retry_detail.text
    _reset()


def test_legacy_preview_error_keeps_retry_action_available(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import ImportRun

        run = ImportRun(
            subject_id=1,
            source="fakturoid",
            status="error",
            file_name="missing.zip",
            file_path="subject-1/missing.zip",
            file_sha256="0" * 64,
        )
        db.add(run)
        db.commit()
        run_id = int(run.id)

    detail = client.get(f"/imports/{run_id}")
    assert "Preview selhalo" in detail.text
    assert "Spustit import" in detail.text
    assert "Import nelze spustit" not in detail.text
    _reset()


def test_strict_import_detail_and_process_are_tenant_scoped(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    payload = _manifest_zip(contacts=b'{"name":"Tenant two"}\n')
    with SessionLocal() as db:
        from fakturek.models import ImportRun

        run = ImportRun(
            subject_id=2,
            source="fakturek_native_v1",
            status="uploaded",
            file_name="tenant-two.zip",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
            mime_type="application/zip",
        )
        db.add(run)
        db.flush()
        stored = import_root / f"subject-2/run-{run.id}.zip"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(payload)
        run.file_path = stored.relative_to(import_root).as_posix()
        db.commit()
        run_id = int(run.id)

    assert client.get(f"/imports/{run_id}").status_code == 404
    assert client.post(f"/imports/{run_id}/process").status_code == 404
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportRun

        assert db.get(ImportRun, run_id).status == "uploaded"
        assert db.query(Contact).filter_by(subject_id=2).count() == 0
    _reset()


def test_native_backup_upload_and_process_require_csrf(monkeypatch, tmp_path):
    client, _SessionLocal, _import_root = _setup(monkeypatch, tmp_path, csrf=True)
    payload = _manifest_zip()
    denied = client.post(
        "/imports",
        data={"source": "fakturek_native_v1"},
        files={"file": ("backup.zip", payload, "application/zip")},
    )
    assert denied.status_code == 403
    run_id = _upload(client, payload, csrf_token=_csrf(client))
    denied_process = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert denied_process.status_code == 403
    _reset()


def test_native_backup_reuses_existing_contacts_and_catalog_items(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    contacts = b'{"name":"Native customer","email":"customer@example.test"}\n'
    catalog = (
        b'{"description":"Native service","quantity":"2.00","unit":"hour",'
        b'"unit_price_cents":12345,"vat_rate":"21.00","currency":"CZK"}\n'
    )
    payload = _manifest_zip(contacts=contacts, catalog=catalog)
    stored = import_root / "conflict.zip"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    run = SimpleNamespace(
        subject_id=1,
        file_path="conflict.zip",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        summary_json=json.dumps({"config": {"contact_conflict_mode": "skip_existing"}}),
    )
    with SessionLocal() as db:
        from fakturek.models import Contact, InvoiceCatalogItem
        from fakturek.native_backup import process_native_backup_import

        summary = process_native_backup_import(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        db.commit()
        contact = db.query(Contact).filter_by(subject_id=1, name="Native customer").one()
        assert contact.email == "customer@example.test"
        assert summary["contacts"]["skipped_existing"] == 1
        assert summary["catalog_items"]["reused"] == 1
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=1).count() == 1
    _reset()


def test_native_backup_plan_preserves_distinct_same_name_contacts(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    contacts = (
        b'{"name":"Same name","email":"first@example.test"}\n'
        b'{"name":"Same name","email":"second@example.test"}\n'
    )
    payload = _manifest_zip(contacts=contacts)
    stored = import_root / "two-same-names.zip"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    run = SimpleNamespace(
        subject_id=2,
        file_path="two-same-names.zip",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        summary_json=json.dumps({"config": {"contact_conflict_mode": "merge_existing"}}),
    )
    with SessionLocal() as db:
        from fakturek.models import Contact
        from fakturek.native_backup import (
            preview_native_backup_import,
            process_native_backup_import,
        )

        preview = preview_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        summary = process_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        db.commit()
        assert preview["contacts"]["will_create"] == summary["contacts"]["created"] == 2
        assert preview["contacts"]["will_reuse"] == summary["contacts"]["reused"] == 0
        assert {
            row.email
            for row in db.query(Contact).filter_by(subject_id=2).order_by(Contact.email).all()
        } == {"first@example.test", "second@example.test"}
    _reset()


def test_native_backup_does_not_name_fallback_after_an_email_miss(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    contacts = (
        b'{"name":"Same name","email":"first@example.test"}\n'
        b'{"name":"Same name","email":"second@example.test"}\n'
    )
    payload = _manifest_zip(contacts=contacts)
    stored = import_root / "preexisting-name-collision.zip"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    run = SimpleNamespace(
        subject_id=2,
        file_path="preexisting-name-collision.zip",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        summary_json=json.dumps({"config": {"contact_conflict_mode": "merge_existing"}}),
    )
    with SessionLocal() as db:
        from fakturek.models import Contact
        from fakturek.native_backup import (
            preview_native_backup_import,
            process_native_backup_import,
        )

        db.add(Contact(subject_id=2, name="Same name", email="first@example.test"))
        db.commit()
        preview = preview_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        summary = process_native_backup_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        db.commit()
        assert preview["contacts"]["will_create"] == summary["contacts"]["created"] == 1
        assert preview["contacts"]["will_reuse"] == summary["contacts"]["reused"] == 1
        assert {
            row.email
            for row in db.query(Contact).filter_by(subject_id=2).order_by(Contact.email).all()
        } == {"first@example.test", "second@example.test"}
    _reset()


def test_native_backup_planning_bulk_loads_maps_instead_of_querying_each_row(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    contacts = b"".join(
        (
            json.dumps({"name": f"Bulk {number}", "email": f"bulk-{number}@example.test"}) + "\n"
        ).encode()
        for number in range(1_200)
    )
    payload = _manifest_zip(contacts=contacts)
    stored = import_root / "bulk-plan.zip"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    run = SimpleNamespace(
        subject_id=2,
        file_path="bulk-plan.zip",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        summary_json="",
    )
    with SessionLocal() as db:
        from sqlalchemy import event

        from fakturek.native_backup import build_native_import_plan

        statements: list[str] = []

        def record(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.bind, "before_cursor_execute", record)
        try:
            plan = build_native_import_plan(
                db,
                run=run,
                subject_id=2,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
            )
        finally:
            event.remove(db.bind, "before_cursor_execute", record)
        assert len(plan.contacts) == 1_200
        # Subject + two snapshots + three 500-row ImportMap chunks; a per-row
        # lookup would exceed this by orders of magnitude.
        assert len(statements) <= 7
    _reset()


def test_native_backup_export_limits_are_import_compatible():
    from fakturek.native_backup import build_native_backup_bytes

    contact = SimpleNamespace(name="Limit", registry_auto_update=True)
    payload = build_native_backup_bytes(
        contacts=[contact],
        catalog_items=[],
        max_rows=1,
        max_member_bytes=10_000,
        max_archive_bytes=10_000,
    )
    assert payload.startswith(b"PK")
    with pytest.raises(ValueError, match="at most 1 rows"):
        build_native_backup_bytes(
            contacts=[contact, contact],
            catalog_items=[],
            max_rows=1,
            max_member_bytes=10_000,
            max_archive_bytes=10_000,
        )
    with pytest.raises(ValueError, match="upload size limit"):
        build_native_backup_bytes(
            contacts=[contact],
            catalog_items=[],
            max_rows=1,
            max_member_bytes=10_000,
            max_archive_bytes=1,
        )


def test_native_backup_process_claim_allows_only_one_concurrent_application(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    contacts = b'{"name":"Concurrent customer","email":"concurrent@example.test"}\n'
    catalog = (
        b'{"description":"Concurrent catalog","quantity":"1.00","unit":"piece",'
        b'"unit_price_cents":100,"vat_rate":"0.00","currency":"CZK"}\n'
    )
    run_id = _upload(client, _manifest_zip(contacts=contacts, catalog=catalog))
    import fakturek.main as main_module

    original = main_module.process_native_backup_import
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def delayed_process(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "process_native_backup_import", delayed_process)
    responses: list[int] = []

    def submit():
        with TestClient(client.app) as concurrent_client:
            response = concurrent_client.post(f"/imports/{run_id}/process", follow_redirects=False)
            responses.append(response.status_code)

    first = threading.Thread(target=submit)
    first.start()
    assert started.wait(timeout=5)
    second = threading.Thread(target=submit)
    second.start()
    second.join(timeout=5)
    release.set()
    first.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert calls == 1
    assert responses == [303, 303]
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, InvoiceCatalogItem

        assert db.query(Contact).filter_by(subject_id=1, name="Concurrent customer").count() == 1
        assert (
            db.query(InvoiceCatalogItem)
            .filter_by(subject_id=1, description="Concurrent catalog")
            .count()
            == 1
        )
        assert db.query(ImportMap).filter_by(subject_id=1, source="fakturek_native_v1").count() == 2
    _reset()


def test_native_backup_process_failure_rolls_back_rows_before_marking_error(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    contacts = b'{"name":"Rollback customer","email":"rollback@example.test"}\n'
    run_id = _upload(client, _manifest_zip(contacts=contacts))
    import fakturek.main as main_module

    def fail_after_first_flush(db, **_kwargs):
        from fakturek.models import Contact

        db.add(Contact(subject_id=1, name="Should roll back", email="no@example.test"))
        db.flush()
        raise RuntimeError("injected native backup failure")

    monkeypatch.setattr(main_module, "process_native_backup_import", fail_after_first_flush)
    response = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun, InvoiceCatalogItem

        assert db.query(Contact).filter_by(subject_id=1, name="Should roll back").count() == 0
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=1).count() == 1
        assert db.query(ImportMap).filter_by(subject_id=1, source="fakturek_native_v1").count() == 0
        assert db.get(ImportRun, run_id).status == "error"
    _reset()


def test_native_backup_route_guard_rejects_hosted_shadowing(monkeypatch, tmp_path):
    _client, _SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    from fastapi import FastAPI

    from fakturek.main import _assert_unique_export_import_routes

    app = FastAPI()

    @app.get("/exports/native-backup.zip")
    def first_export():
        return {"ok": True}

    @app.get("/exports/native-backup.zip")
    def shadow_export():
        return {"ok": False}

    with pytest.raises(RuntimeError, match="Duplicate export/import route"):
        _assert_unique_export_import_routes(app)
    _reset()

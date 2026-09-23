from __future__ import annotations

import hashlib
from decimal import Decimal

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
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'catalog.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    _reset()
    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import InvoiceCatalogItem, Subject

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all([Subject(id=1, name="One"), Subject(id=2, name="Two")])
        db.add(
            InvoiceCatalogItem(
                subject_id=1,
                description="Exact row",
                quantity=Decimal("1"),
                unit="hour",
                unit_price_cents=100,
                vat_rate=Decimal("21"),
                currency="CZK",
            )
        )
        db.commit()
    return TestClient(create_app()), SessionLocal, tmp_path / "imports"


def _csv(rows: list[str]) -> bytes:
    return (
        b"\xef\xbb\xbf"
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n"
        + "\n".join(rows).encode("utf-8")
        + b"\n"
    )


def test_catalog_csv_export_import_preview_variants_and_idempotence(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    exported = client.get("/exports/catalog-items.csv")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")

    from fakturek.catalog_csv import (
        SOURCE,
        build_catalog_csv_import_plan,
        preview_catalog_csv_import,
        process_catalog_csv_import,
    )
    from fakturek.models import ImportMap, ImportRun, InvoiceCatalogItem

    payload = _csv(
        [
            "1;Exact row;1;hour;1;21;CZK",
            "1;Exact row;1;hour;2;21;CZK",
            "1;Exact row;1;hour;2;21;CZK",
        ]
    )
    import_root.mkdir()
    stored = import_root / "subject-1" / "catalog.csv"
    stored.parent.mkdir()
    stored.write_bytes(payload)
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="subject-1/catalog.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
        )
        db.add(run)
        db.commit()
        plan = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        preview = preview_catalog_csv_import(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
            plan=plan,
        )
        assert preview["catalog_items"] == {
            "parsed": 3,
            "will_create": 1,
            "will_reuse": 1,
            "duplicate_rows": 1,
        }
        summary = process_catalog_csv_import(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
            plan=plan,
        )
        db.commit()
        assert summary["catalog_items"] == {
            "parsed": 3,
            "created": 1,
            "reused": 1,
            "duplicate_rows": 1,
        }
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=1).count() == 2
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=2).count() == 0
        assert (
            db.query(ImportMap)
            .filter_by(subject_id=1, source=SOURCE, entity_type="catalog_item")
            .count()
            == 2
        )

        repeat = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        assert [action.action for action in repeat.actions] == ["reuse", "reuse", "duplicate"]
    _reset()


def test_invalid_catalog_preview_hides_process_action_and_remains_fail_closed(
    monkeypatch, tmp_path
):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    valid = client.post(
        "/imports",
        data={"source": "fakturek_catalog_csv_v1"},
        files={
            "file": (
                "catalog.csv",
                _csv(["1;Valid row;1;hour;1;21;CZK"]),
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert "Spustit import" in client.get(valid.headers["location"]).text

    payload = b"wrong;header\n1;unsafe\n"
    uploaded = client.post(
        "/imports",
        data={"source": "fakturek_catalog_csv_v1"},
        files={"file": ("catalog.csv", payload, "text/csv")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    detail_url = uploaded.headers["location"]
    run_id = int(detail_url.split("/")[2].split("?")[0])
    detail = client.get(detail_url)
    assert "Preview selhalo" in detail.text
    assert "Import nelze spustit" in detail.text
    assert "Spustit import" not in detail.text
    assert f'action="/imports/{run_id}/process"' not in detail.text

    processed = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert processed.status_code == 303
    assert processed.headers["location"] == f"/imports/{run_id}?error=1"
    with SessionLocal() as db:
        from fakturek.models import ImportRun, InvoiceCatalogItem

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=1).count() == 1
    retry_detail = client.get(f"/imports/{run_id}")
    assert "Import nelze spustit" in retry_detail.text
    assert "Spustit import" not in retry_detail.text
    _reset()


def test_catalog_csv_processing_uses_bounded_bulk_sql(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    from sqlalchemy import event

    from fakturek.catalog_csv import (
        SOURCE,
        build_catalog_csv_import_plan,
        process_catalog_csv_import,
    )
    from fakturek.models import ImportRun

    payload = _csv([f"1;Bulk {index};1;hour;{index % 100};21;CZK" for index in range(1_000)])
    import_root.mkdir()
    (import_root / "bulk.csv").write_bytes(payload)
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="bulk.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        plan = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        statements: list[str] = []

        def observe(*args):
            statements.append(str(args[2]))

        event.listen(db.bind, "before_cursor_execute", observe)
        process_catalog_csv_import(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
            plan=plan,
        )
        event.remove(db.bind, "before_cursor_execute", observe)
        db.commit()
        assert len(statements) <= 20

        repeat = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        statements.clear()
        event.listen(db.bind, "before_cursor_execute", observe)
        process_catalog_csv_import(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
            plan=repeat,
        )
        event.remove(db.bind, "before_cursor_execute", observe)
        assert len(statements) <= 4
    _reset()


def test_catalog_csv_upload_dedupe_key_is_catalog_only_and_atomic(monkeypatch, tmp_path):
    _client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    from sqlalchemy.exc import IntegrityError

    from fakturek.catalog_csv import SOURCE
    from fakturek.models import ImportRun

    with SessionLocal() as first, SessionLocal() as second:
        first.add(
            ImportRun(
                subject_id=1,
                source=SOURCE,
                status="uploaded",
                upload_dedupe_key="a" * 64,
            )
        )
        first.commit()
        second.add(
            ImportRun(
                subject_id=1,
                source=SOURCE,
                status="uploaded",
                upload_dedupe_key="a" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
        second.add_all(
            [
                ImportRun(subject_id=1, source="fakturoid", status="uploaded"),
                ImportRun(subject_id=1, source="fakturoid", status="uploaded"),
            ]
        )
        second.commit()
    _reset()


def test_catalog_csv_second_chunk_failure_rolls_back_every_catalog_write(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    import fakturek.catalog_csv as catalog_csv  # noqa: I001

    from fakturek.catalog_csv import (
        SOURCE,
        build_catalog_csv_import_plan,
        process_catalog_csv_import,
    )
    from fakturek.models import ImportMap, ImportRun, InvoiceCatalogItem

    payload = _csv([f"1;Rollback {index};1;hour;1;21;CZK" for index in range(501)])
    import_root.mkdir()
    (import_root / "rollback.csv").write_bytes(payload)
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="rollback.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
        )
        db.add(run)
        db.commit()
        plan = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        original = catalog_csv._insert_create_chunk
        calls = 0

        def fail_second_chunk(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second chunk failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(catalog_csv, "_insert_create_chunk", fail_second_chunk)
        with pytest.raises(RuntimeError):
            process_catalog_csv_import(
                db,
                run=run,
                subject_id=1,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
                plan=plan,
            )
        db.rollback()
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=1).count() == 1
        assert db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).count() == 0
    _reset()

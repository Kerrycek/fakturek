from __future__ import annotations

import hashlib
import re
import threading

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module  # noqa: E402, I001
from fakturek.db import Base  # noqa: E402
from fakturek.settings import get_settings  # noqa: E402


SOURCE = "fakturek_contacts_csv_v1"
HEADER = (
    "fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;"
    "ico;dic;fixed_variable_symbol\n"
)


def _reset() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup(monkeypatch, tmp_path, *, csrf: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'contacts.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "1" if csrf else "0")
    monkeypatch.setenv("SECRET_KEY", "contacts-csv-test-secret")
    _reset()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Subject

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all([Subject(id=1, name="One"), Subject(id=2, name="Two")])
        db.add(
            Contact(
                subject_id=1,
                name="Exact",
                email="exact@example.test",
                phone="+420111222333",
                street="One 1",
                city="Praha",
                zip="11000",
                country="CZ",
                ico="12345678",
                dic="CZ12345678",
                fixed_variable_symbol="42",
            )
        )
        db.commit()
    return TestClient(create_app()), SessionLocal, tmp_path / "imports"


def _csv(*rows: str) -> bytes:
    return b"\xef\xbb\xbf" + (HEADER + "\n".join(rows) + "\n").encode("utf-8")


def _csrf(client: TestClient) -> str:
    page = client.get("/imports")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def _upload(client: TestClient, payload: bytes, *, csrf_token: str = "") -> tuple[int, str]:
    response = client.post(
        "/imports",
        data={"source": SOURCE, "csrf_token": csrf_token},
        files={"file": ("contacts.csv", payload, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    run_id = int(response.headers["location"].split("/")[2].split("?")[0])
    return run_id, response.headers["location"]


def test_contacts_csv_ui_preview_process_is_scoped_non_overwriting_and_idempotent(
    monkeypatch, tmp_path
):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    payload = _csv(
        "1;Exact;exact@example.test;+420111222333;One 1;Praha;11000;CZ;12345678;CZ12345678;42",
        "1;Exact;different@example.test;;;;;CZ;;;",
        "1;'=Formula;formula@example.test;;;;;CZ;;;0007",
        "1;'=Formula;formula@example.test;;;;;CZ;;;0007",
    )
    run_id, detail_url = _upload(client, payload)
    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Fakturek kontakty CSV v1" in detail.text
    assert "Co se stane při importu kontaktů" in detail.text
    assert "Existující kontakty se nikdy nepřepisují" in detail.text
    assert "Nové kontakty" in detail.text
    assert "Duplicitní řádky" in detail.text
    assert f'action="/imports/{run_id}/process"' in detail.text

    active_duplicate_id, active_duplicate_url = _upload(client, payload)
    assert active_duplicate_id == run_id
    assert "duplicate=1" in active_duplicate_url
    generic = client.post(
        "/imports",
        data={"source": "contacts_csv"},
        files={"file": ("contacts.csv", payload, "text/csv")},
        follow_redirects=False,
    )
    assert generic.status_code == 303
    assert int(generic.headers["location"].split("/")[2].split("?")[0]) != run_id

    switched = client.post(
        "/settings/language",
        data={"ui_language": "en", "next": f"/imports/{run_id}"},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    english_list = client.get("/imports")
    english_detail = client.get(f"/imports/{run_id}")
    assert "<strong>Fakturek Contacts CSV v1</strong>" in english_list.text
    assert "What will happen when contacts are imported" in english_detail.text
    assert "Existing contacts are never overwritten" in english_detail.text
    assert client.post(
        "/settings/language",
        data={"ui_language": "cs", "next": f"/imports/{run_id}"},
        follow_redirects=False,
    ).status_code == 303

    processed = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert processed.status_code == 303
    assert processed.headers["location"] == f"/imports/{run_id}?processed=1"

    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun

        exact = db.query(Contact).filter_by(subject_id=1, email="exact@example.test").one()
        assert exact.phone == "+420111222333"
        assert exact.fixed_variable_symbol == "42"
        assert db.query(Contact).filter_by(subject_id=1, name="Exact").count() == 2
        formula = db.query(Contact).filter_by(subject_id=1, email="formula@example.test").one()
        assert formula.name == "=Formula"
        assert formula.fixed_variable_symbol == "0007"
        assert db.query(Contact).filter_by(subject_id=2).count() == 0
        assert (
            db.query(ImportMap)
            .filter_by(subject_id=1, source=SOURCE, entity_type="contact")
            .count()
            == 3
        )
        assert db.query(ImportMap).filter_by(source="contacts_csv").count() == 0
        summary = db.get(ImportRun, run_id).summary_json
        assert '"created": 2' in summary
        assert '"reused": 1' in summary
        assert '"duplicate_rows": 1' in summary
        exact.phone = "LOCAL-EDIT"
        db.add(exact)
        db.commit()

    replay_id, replay_url = _upload(client, payload)
    assert replay_id != run_id
    replay = client.get(replay_url)
    assert "Znovupoužité kontakty" in replay.text
    assert "Mapované kontakty se zachovanými úpravami" in replay.text
    replay_processed = client.post(f"/imports/{replay_id}/process", follow_redirects=False)
    assert replay_processed.status_code == 303
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap

        assert db.query(Contact).filter_by(subject_id=1).count() == 3
        assert db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).count() == 3
        exact = db.query(Contact).filter_by(subject_id=1, email="exact@example.test").one()
        assert exact.phone == "LOCAL-EDIT"
        replay_summary = db.get(ImportRun, replay_id).summary_json
        assert '"mapped_preserved": 1' in replay_summary
    _reset()


@pytest.mark.parametrize(
    "payload",
    [
        b"wrong;header\n1;unsafe\n",
        _csv("2;Wrong version;;;;;;;;;"),
        b"\xef\xbb\xbf" + HEADER.encode("utf-8") + b"1;Bad\xff;;;;;;;;;\n",
        _csv('1;"unterminated;;;;;;;;;'),
    ],
)
def test_invalid_contacts_csv_preview_blocks_ui_and_direct_process(monkeypatch, tmp_path, payload):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    run_id, detail_url = _upload(client, payload)
    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert "Preview selhalo" in detail.text
    assert "Import nelze spustit" in detail.text
    assert "Spustit import" not in detail.text
    assert f'action="/imports/{run_id}/process"' not in detail.text

    response = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/imports/{run_id}?error=1"
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(Contact).filter_by(subject_id=1).count() == 1
        assert db.query(ImportMap).filter_by(source=SOURCE).count() == 0
    _reset()


def test_contacts_csv_plan_revalidates_file_tenant_destination_and_maps(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    from fakturek.contacts_csv import (
        build_contacts_csv_import_plan,
        process_contacts_csv_import,
    )
    from fakturek.models import Contact, ImportMap, ImportRun

    payload = _csv("1;Planned;planned@example.test;;;;;CZ;;;")
    import_root.mkdir()
    stored = import_root / "planned.csv"
    stored.write_bytes(payload)
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="planned.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
        )
        db.add(run)
        db.commit()
        plan = build_contacts_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        db.add(Contact(subject_id=1, name="Concurrent edit"))
        db.commit()
        with pytest.raises(ValueError, match="destination changed"):
            process_contacts_csv_import(
                db,
                run=run,
                subject_id=1,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
                plan=plan,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=1, name="Planned").count() == 0
        assert db.query(ImportMap).filter_by(source=SOURCE).count() == 0

        concurrent = db.query(Contact).filter_by(subject_id=1, name="Concurrent edit").one()
        db.delete(concurrent)
        db.commit()
        map_plan = build_contacts_csv_import_plan(
            db,
            run=run,
            subject_id=1,
            import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        exact = db.query(Contact).filter_by(subject_id=1, name="Exact").one()
        db.add(
            ImportMap(
                subject_id=1,
                source=SOURCE,
                entity_type="contact",
                external_id=map_plan.actions[0].identity,
                internal_id=int(exact.id),
            )
        )
        db.commit()
        with pytest.raises(ValueError, match="mappings changed"):
            process_contacts_csv_import(
                db,
                run=run,
                subject_id=1,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
                plan=map_plan,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=1, name="Planned").count() == 0
        with pytest.raises(ValueError, match="current subject"):
            build_contacts_csv_import_plan(
                db,
                run=run,
                subject_id=2,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
            )
    _reset()


def test_contacts_csv_direct_process_revalidates_stored_file_after_preview(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    run_id, detail_url = _upload(
        client,
        _csv("1;File guard;guard@example.test;;;;;CZ;;;"),
    )
    assert "Spustit import" in client.get(detail_url).text
    with SessionLocal() as db:
        from fakturek.models import ImportRun

        run = db.get(ImportRun, run_id)
        stored = (import_root / run.file_path).resolve()
    stored.write_bytes(_csv("1;Changed;changed@example.test;;;;;CZ;;;"))

    response = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/imports/{run_id}?error=1"
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(Contact).filter(Contact.name.in_(["File guard", "Changed"])).count() == 0
        assert db.query(ImportMap).filter_by(source=SOURCE).count() == 0
    _reset()


def test_contacts_csv_plan_is_bound_to_exact_run_metadata(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    from fakturek.contacts_csv import build_contacts_csv_import_plan, process_contacts_csv_import
    from fakturek.models import ImportRun

    payload = _csv("1;Bound;bound@example.test;;;;;CZ;;;")
    import_root.mkdir()
    (import_root / "bound.csv").write_bytes(payload)
    with SessionLocal() as db:
        first = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="bound.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
        )
        second = ImportRun(
            subject_id=1,
            source=SOURCE,
            status="uploaded",
            file_path="bound.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
        )
        db.add_all([first, second])
        db.commit()
        plan = build_contacts_csv_import_plan(
            db, run=first, subject_id=1, import_storage_root=import_root,
            max_upload_bytes=100 * 1024 * 1024,
        )
        with pytest.raises(ValueError, match="does not match"):
            process_contacts_csv_import(
                db, run=second, subject_id=1, import_storage_root=import_root,
                max_upload_bytes=100 * 1024 * 1024, plan=plan,
            )
        first.file_sha256 = ""
        db.add(first)
        db.commit()
        with pytest.raises(ValueError, match="does not match|checksum is invalid"):
            process_contacts_csv_import(
                db, run=first, subject_id=1, import_storage_root=import_root,
                max_upload_bytes=100 * 1024 * 1024, plan=plan,
            )
        with pytest.raises(ValueError, match="checksum is invalid"):
            build_contacts_csv_import_plan(
                db, run=first, subject_id=1, import_storage_root=import_root,
                max_upload_bytes=100 * 1024 * 1024,
            )
    _reset()


def test_contacts_csv_processing_uses_bounded_batch_sql(monkeypatch, tmp_path):
    _client, SessionLocal, import_root = _setup(monkeypatch, tmp_path)
    from sqlalchemy import event

    from fakturek.contacts_csv import build_contacts_csv_import_plan, process_contacts_csv_import
    from fakturek.models import ImportRun

    payload = _csv(
        *(f"1;Bulk {index};bulk-{index}@example.test;;;;;CZ;;;" for index in range(1000))
    )
    import_root.mkdir()
    (import_root / "bulk.csv").write_bytes(payload)
    with SessionLocal() as db:
        run = ImportRun(
            subject_id=1, source=SOURCE, status="uploaded", file_path="bulk.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(), file_size_bytes=len(payload),
        )
        db.add(run)
        db.commit()
        plan = build_contacts_csv_import_plan(
            db, run=run, subject_id=1, import_storage_root=import_root,
            max_upload_bytes=25 * 1024 * 1024,
        )
        statements: list[str] = []

        def observe(*args):
            statements.append(str(args[2]))

        event.listen(db.bind, "before_cursor_execute", observe)
        try:
            process_contacts_csv_import(
                db, run=run, subject_id=1, import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024, plan=plan,
            )
        finally:
            event.remove(db.bind, "before_cursor_execute", observe)
        assert len(statements) <= 20
        db.rollback()
    _reset()


def test_contacts_csv_repairs_orphan_map_to_manual_recreation_then_recreates_after_delete(
    monkeypatch, tmp_path
):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    payload = _csv("1;Restored;restore@example.test;;;;;CZ;;;")
    first_id, _ = _upload(client, payload)
    assert client.post(f"/imports/{first_id}/process", follow_redirects=False).status_code == 303

    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap

        original = db.query(Contact).filter_by(subject_id=1, name="Restored").one()
        mapping = db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).one()
        original_id = int(original.id)
        db.delete(original)
        db.commit()
        manual = Contact(subject_id=1, name="Restored", email="restore@example.test", country="CZ")
        db.add(manual)
        db.commit()
        manual_id = int(manual.id)
        assert int(mapping.internal_id) == original_id

    second_id, second_url = _upload(client, payload)
    assert "Osiřelá mapování k opravě" in client.get(second_url).text
    assert client.post(f"/imports/{second_id}/process", follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap

        mapping = db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).one()
        assert int(mapping.internal_id) == manual_id
        assert db.query(Contact).filter_by(subject_id=1, name="Restored").count() == 1
        db.delete(db.get(Contact, manual_id))
        db.commit()

    third_id, _ = _upload(client, payload)
    assert client.post(f"/imports/{third_id}/process", follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap

        recreated = db.query(Contact).filter_by(subject_id=1, name="Restored").one()
        mapping = db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).one()
        assert int(mapping.internal_id) == int(recreated.id)
        assert recreated.email == "restore@example.test"
    _reset()


def test_contacts_csv_failure_rolls_back_all_rows_and_maps(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    payload = _csv(
        "1;Rollback one;one@example.test;;;;;CZ;;;",
        "1;Rollback two;two@example.test;;;;;CZ;;;",
    )
    run_id, _ = _upload(client, payload)
    import fakturek.contacts_csv as contacts_csv

    original = contacts_csv._insert_create_chunk

    def fail_after_chunk(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected contacts CSV failure")

    monkeypatch.setattr(contacts_csv, "_insert_create_chunk", fail_after_chunk)
    response = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/imports/{run_id}?error=1"
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(Contact).filter(Contact.name.like("Rollback %")).count() == 0
        assert db.query(ImportMap).filter_by(source=SOURCE).count() == 0
    _reset()


def test_contacts_csv_process_claim_allows_only_one_concurrent_application(monkeypatch, tmp_path):
    client, SessionLocal, _import_root = _setup(monkeypatch, tmp_path)
    run_id, _ = _upload(client, _csv("1;Concurrent;concurrent@example.test;;;;;CZ;;;"))
    import fakturek.main as main_module

    original = main_module.process_contacts_csv_import
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def delayed(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "process_contacts_csv_import", delayed)
    responses: list[int] = []

    def submit():
        with TestClient(client.app) as concurrent_client:
            response = concurrent_client.post(
                f"/imports/{run_id}/process", follow_redirects=False
            )
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
        from fakturek.models import Contact, ImportMap

        assert db.query(Contact).filter_by(subject_id=1, name="Concurrent").count() == 1
        assert db.query(ImportMap).filter_by(subject_id=1, source=SOURCE).count() == 1
    _reset()


def test_contacts_csv_upload_process_csrf_and_foreign_tenant_guards(monkeypatch, tmp_path):
    client, SessionLocal, import_root = _setup(monkeypatch, tmp_path, csrf=True)
    payload = _csv("1;CSRF;csrf@example.test;;;;;CZ;;;")
    denied = client.post(
        "/imports",
        data={"source": SOURCE},
        files={"file": ("contacts.csv", payload, "text/csv")},
    )
    assert denied.status_code == 403
    token = _csrf(client)
    run_id, _ = _upload(client, payload, csrf_token=token)
    assert client.post(f"/imports/{run_id}/process", follow_redirects=False).status_code == 403

    from fakturek.contacts_csv import build_contacts_csv_import_plan
    from fakturek.models import ImportRun

    stored = import_root / "subject-2" / "foreign.csv"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    with SessionLocal() as db:
        foreign = ImportRun(
            subject_id=2,
            source=SOURCE,
            status="uploaded",
            file_path="subject-2/foreign.csv",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
        )
        db.add(foreign)
        db.commit()
        foreign_id = int(foreign.id)
        with pytest.raises(ValueError, match="current subject"):
            build_contacts_csv_import_plan(
                db,
                run=foreign,
                subject_id=1,
                import_storage_root=import_root,
                max_upload_bytes=25 * 1024 * 1024,
            )
    assert client.get(f"/imports/{foreign_id}").status_code == 404
    assert (
        client.post(
            f"/imports/{foreign_id}/process",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        ).status_code
        == 404
    )
    _reset()

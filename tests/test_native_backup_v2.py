from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings

IBAN = "CZ6508000000192000145399"


def _reset() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'v2.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "v2-test-secret")
    _reset()
    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.models import Contact, InvoiceCatalogItem, Subject, SubjectBankAccount

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all([Subject(id=1, name="Source"), Subject(id=2, name="Destination")])
        db.add(Contact(subject_id=1, name="Customer", email="customer@example.test"))
        db.add(
            InvoiceCatalogItem(
                subject_id=1,
                description="Service",
                quantity="1.00",
                unit="ks",
                unit_price_cents=100,
                vat_rate="21.00",
                currency="CZK",
            )
        )
        db.add(
            SubjectBankAccount(
                subject_id=1,
                label="Main",
                account_number="19-2000145399/0800",
                iban=IBAN,
                bic="GIBACZPX",
                country="CZ",
                currency="CZK",
                is_default=True,
                sort_order=3,
                fio_api_token="super-secret-token",
                payment_sync_provider="fio_api",
                payment_sync_enabled=True,
                payment_sync_last_error="never export",
                payment_sync_last_email_uid="999",
            )
        )
        db.commit()
    return SessionLocal, tmp_path / "imports"


def _payload(SessionLocal):
    from fakturek.models import Contact, InvoiceCatalogItem, SubjectBankAccount
    from fakturek.native_backup_v2 import build_native_backup_v2_bytes

    with SessionLocal() as db:
        return build_native_backup_v2_bytes(
            contacts=list(db.query(Contact).filter_by(subject_id=1)),
            catalog_items=list(db.query(InvoiceCatalogItem).filter_by(subject_id=1)),
            bank_accounts=list(db.query(SubjectBankAccount).filter_by(subject_id=1)),
            max_member_bytes=10_000_000,
            max_archive_bytes=10_000_000,
        )


def _run(db, root: Path, payload: bytes, *, subject_id: int = 2):
    from fakturek.models import ImportRun

    run = ImportRun(
        subject_id=subject_id,
        source="fakturek_native_v2",
        status="uploaded",
        file_name="backup-v2.zip",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        file_size_bytes=len(payload),
        mime_type="application/zip",
    )
    db.add(run)
    db.flush()
    target = root / f"subject-{subject_id}" / f"run-{run.id}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    run.file_path = target.relative_to(root).as_posix()
    db.flush()
    return run


def test_v2_round_trip_excludes_secrets_and_replays_without_duplicates(
    monkeypatch, tmp_path
):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    assert b"super-secret-token" not in payload
    assert b"never export" not in payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "contacts.jsonl",
            "catalog_items.jsonl",
            "bank_accounts.jsonl",
        ]
        row = json.loads(archive.read("bank_accounts.jsonl"))
        assert set(row) == {
            "label",
            "account_number",
            "iban",
            "bic",
            "country",
            "currency",
            "is_default",
            "sort_order",
        }
        assert archive.read("manifest.json").find(b"fio_api_token") == -1
    with SessionLocal() as db:
        from fakturek.models import (
            Contact,
            ImportMap,
            InvoiceCatalogItem,
            Subject,
            SubjectBankAccount,
        )
        from fakturek.native_backup_v2 import process_native_backup_v2_import

        run = _run(db, root, payload)
        subject_updated_at = db.get(Subject, 2).updated_at
        process_native_backup_v2_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.commit()
        process_native_backup_v2_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.commit()
        account = db.query(SubjectBankAccount).filter_by(subject_id=2).one()
        assert (
            account.fio_api_token is None
            and account.payment_sync_provider == "none"
            and not account.payment_sync_enabled
        )
        assert db.query(Contact).filter_by(subject_id=2).count() == 1
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=2).count() == 1
        assert (
            db.query(ImportMap)
            .filter_by(subject_id=2, source="fakturek_native_v2")
            .count()
            == 3
        )
        assert db.get(Subject, 2).updated_at == subject_updated_at
    _reset()


def test_v2_import_plan_freezes_backup_and_action_rows(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    from fakturek.native_backup_v2 import (
        build_native_backup_v2_import_plan,
        parse_native_backup_v2_bytes,
    )

    backup = parse_native_backup_v2_bytes(
        payload, max_member_bytes=10_000_000, max_total_bytes=10_000_000
    )
    with pytest.raises(TypeError):
        backup.manifest["version"] = 99
    with pytest.raises(TypeError):
        backup.manifest["datasets"][0]["row_count"] = 99
    with pytest.raises(AttributeError):
        backup.contacts.append({})
    with pytest.raises(TypeError):
        backup.contacts[0]["name"] = "changed"

    with SessionLocal() as db:
        plan = build_native_backup_v2_import_plan(
            db,
            run=_run(db, root, payload),
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        with pytest.raises(TypeError):
            plan.contacts[0].row["name"] = "changed"
    _reset()


def test_v2_export_route_and_upload_require_csrf(monkeypatch, tmp_path):
    _SessionLocal, _root = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CSRF_ENABLED", "1")
    get_settings.cache_clear()
    from fakturek.main import create_app

    client = TestClient(create_app())
    exported = client.get("/exports/native-backup-v2.zip")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.fakturek.native-backup-v2+zip"
    )
    denied = client.post(
        "/imports",
        data={"source": "fakturek_native_v2"},
        files={"file": ("backup.zip", exported.content, "application/zip")},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    page = client.get("/imports")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token
    accepted = client.post(
        "/imports",
        data={"source": "fakturek_native_v2", "csrf_token": token.group(1)},
        files={"file": ("backup.zip", exported.content, "application/zip")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    detail = client.get(accepted.headers["location"])
    assert detail.status_code == 200
    from fakturek.ui_i18n import translate_html_document

    english_detail = translate_html_document(detail.text, "en")
    for expected in (
        "Verified backup contents",
        "New accounts",
        "Will become default",
        "Accounts checksum",
        "The ZIP was fully validated.",
        "The preview is current but non-binding",
        "New accounts preserve their relative backup order",
    ):
        assert expected in english_detail
    assert "Účty nové" not in english_detail
    _reset()


def test_v2_match_never_overwrites_and_default_conflict_warns(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    with SessionLocal() as db:
        from fakturek.models import SubjectBankAccount

        db.add(
            SubjectBankAccount(
                subject_id=2,
                label="Do not change",
                account_number="different",
                iban=IBAN,
                bic="OTHERBIC",
                country="CZ",
                currency="CZK",
                is_default=True,
                sort_order=77,
                fio_api_token="destination-secret",
            )
        )
        db.commit()
        from fakturek.native_backup_v2 import (
            build_native_backup_v2_import_plan,
            process_native_backup_v2_import,
        )

        run = _run(db, root, payload)
        plan = build_native_backup_v2_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert (
            plan.bank_accounts[0].action == "reuse"
            and not plan.bank_accounts[0].import_default
            and plan.warnings
        )
        process_native_backup_v2_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
            plan=plan,
        )
        db.commit()
        account = db.query(SubjectBankAccount).filter_by(subject_id=2).one()
        assert (
            account.label == "Do not change"
            and account.account_number == "different"
            and account.fio_api_token == "destination-secret"
            and account.sort_order == 77
        )
    _reset()


def _archive_with_account(
    row: dict, *, extra_member: str | None = None, checksum: str | None = None
) -> bytes:
    contacts = b""
    catalog = b""
    accounts = json.dumps(row, separators=(",", ":")).encode() + b"\n"
    entries = [
        ("contacts", "contacts.jsonl", contacts),
        ("catalog_items", "catalog_items.jsonl", catalog),
        ("bank_accounts", "bank_accounts.jsonl", accounts),
    ]
    manifest = {
        "format": "fakturek-native-backup",
        "version": 2,
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "export_id": str(uuid4()),
        "datasets": [
            {
                "name": n,
                "filename": f,
                "schema_version": 1,
                "row_count": data.count(b"\n"),
                "sha256": (
                    checksum
                    if n == "bank_accounts" and checksum is not None
                    else hashlib.sha256(data).hexdigest()
                ),
            }
            for n, f, data in entries
        ],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        [archive.writestr(f, data) for _n, f, data in entries]
        if extra_member:
            archive.writestr(extra_member, b"x")
    return out.getvalue()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "bad_checksum",
        "traversal",
        "duplicate_identity",
        "two_defaults",
    ],
)
def test_v2_rejects_malformed_or_unsafe_account_archives(mutation):
    from fakturek.native_backup_v2 import parse_native_backup_v2_bytes

    row = {
        "label": "A",
        "account_number": "",
        "iban": IBAN,
        "bic": "GIBACZPX",
        "country": "CZ",
        "currency": "CZK",
        "is_default": True,
        "sort_order": 0,
    }
    if mutation == "unknown_field":
        row["fio_api_token"] = "no"
    if mutation == "duplicate_identity":
        # A valid archive with a duplicated row is intentionally rejected.
        payload = _archive_with_account(row)
        with zipfile.ZipFile(io.BytesIO(payload)) as original:
            files = {
                info.filename: original.read(info.filename)
                for info in original.infolist()
            }
        files["bank_accounts.jsonl"] += files["bank_accounts.jsonl"]
        manifest = json.loads(files["manifest.json"])
        item = next(x for x in manifest["datasets"] if x["name"] == "bank_accounts")
        item["row_count"] = 2
        item["sha256"] = hashlib.sha256(files["bank_accounts.jsonl"]).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode()
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        payload = out.getvalue()
    elif mutation == "two_defaults":
        payload = _archive_with_account(row)
        with zipfile.ZipFile(io.BytesIO(payload)) as original:
            files = {
                info.filename: original.read(info.filename)
                for info in original.infolist()
            }
        other = {**row, "iban": "", "account_number": "456/0800"}
        files["bank_accounts.jsonl"] += (
            json.dumps(other, separators=(",", ":")).encode() + b"\n"
        )
        manifest = json.loads(files["manifest.json"])
        item = next(x for x in manifest["datasets"] if x["name"] == "bank_accounts")
        item["row_count"] = 2
        item["sha256"] = hashlib.sha256(files["bank_accounts.jsonl"]).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode()
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        payload = out.getvalue()
    else:
        payload = _archive_with_account(
            row,
            extra_member="../bad" if mutation == "traversal" else None,
            checksum="0" * 64 if mutation == "bad_checksum" else None,
        )
    with pytest.raises(ValueError):
        parse_native_backup_v2_bytes(
            payload, max_member_bytes=10_000_000, max_total_bytes=10_000_000
        )


def test_v2_stale_immutable_plan_aborts_before_any_import(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    with SessionLocal() as db:
        from fakturek.models import Contact, SubjectBankAccount
        from fakturek.native_backup_v2 import (
            build_native_backup_v2_import_plan,
            process_native_backup_v2_import,
        )

        run = _run(db, root, payload)
        plan = build_native_backup_v2_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.add(
            SubjectBankAccount(
                subject_id=2,
                label="Concurrent",
                account_number="123/0800",
                country="CZ",
                currency="CZK",
                sort_order=0,
            )
        )
        db.commit()
        with pytest.raises(ValueError, match="changed since preview"):
            process_native_backup_v2_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
                plan=plan,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=2).count() == 0
    _reset()


def test_v2_process_refreshes_accounts_changed_by_another_session(
    monkeypatch, tmp_path
):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    from fakturek.models import Contact, SubjectBankAccount
    from fakturek.native_backup_v2 import (
        build_native_backup_v2_import_plan,
        process_native_backup_v2_import,
    )

    with SessionLocal() as seed:
        seed.add(
            SubjectBankAccount(
                subject_id=2,
                label="Destination",
                account_number="123/0800",
                country="CZ",
                currency="CZK",
                is_default=False,
                sort_order=4,
            )
        )
        seed.commit()

    with SessionLocal() as db:
        run = _run(db, root, payload)
        db.commit()
        plan = build_native_backup_v2_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert plan.account_snapshot[0][3] == 4

        with SessionLocal() as concurrent:
            account = concurrent.query(SubjectBankAccount).filter_by(subject_id=2).one()
            account.sort_order = 9
            concurrent.commit()

        with pytest.raises(ValueError, match="changed since preview"):
            process_native_backup_v2_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
                plan=plan,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=2).count() == 0
    _reset()


def test_v1_and_v2_archives_reject_each_other(monkeypatch, tmp_path):
    SessionLocal, _root = _setup(monkeypatch, tmp_path)
    from fakturek.native_backup import (
        build_native_backup_bytes,
        parse_native_backup_bytes,
    )
    from fakturek.native_backup_v2 import parse_native_backup_v2_bytes

    v1 = build_native_backup_bytes(
        contacts=[],
        catalog_items=[],
        max_member_bytes=10_000_000,
        max_archive_bytes=10_000_000,
    )
    v2 = _payload(SessionLocal)
    with pytest.raises(ValueError):
        parse_native_backup_bytes(
            v2, max_member_bytes=10_000_000, max_total_bytes=10_000_000
        )
    with pytest.raises(ValueError):
        parse_native_backup_v2_bytes(
            v1, max_member_bytes=10_000_000, max_total_bytes=10_000_000
        )
    _reset()


@pytest.mark.parametrize(
    "row",
    [
        {
            "label": "A",
            "account_number": "",
            "iban": "",
            "bic": "",
            "country": "CZ",
            "currency": "CZK",
            "is_default": False,
            "sort_order": 0,
        },
        {
            "label": "A",
            "account_number": "",
            "iban": IBAN,
            "bic": "",
            "country": "SK",
            "currency": "CZK",
            "is_default": False,
            "sort_order": 0,
        },
        {
            "label": "A",
            "account_number": "1/0800",
            "iban": "",
            "bic": "",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "yes",
            "sort_order": 0,
        },
    ],
)
def test_v2_rejects_bad_account_types_and_identity(row):
    from fakturek.native_backup_v2 import _normalise_bank_account

    with pytest.raises(ValueError):
        _normalise_bank_account(row)


def test_v2_rolls_back_all_business_rows_on_processing_failure(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    with SessionLocal() as db:
        import fakturek.native_backup_v2 as native_v2
        from fakturek.models import Contact, InvoiceCatalogItem, SubjectBankAccount

        run = _run(db, root, payload)
        original_claim = native_v2._claim

        def fail_account_claim(*args, **kwargs):
            if kwargs.get("entity_type") == "bank_account":
                raise RuntimeError("injected failure")
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(native_v2, "_claim", fail_account_claim)
        with pytest.raises(RuntimeError, match="injected failure"):
            native_v2.process_native_backup_v2_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=2).count() == 0
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=2).count() == 0
        assert db.query(SubjectBankAccount).filter_by(subject_id=2).count() == 0
    _reset()


def test_v2_plan_rows_are_immutable(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    with SessionLocal() as db:
        from fakturek.native_backup_v2 import build_native_backup_v2_import_plan

        run = _run(db, root, payload)
        plan = build_native_backup_v2_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        with pytest.raises(TypeError):
            plan.bank_accounts[0].row["label"] = "mutated"  # type: ignore[index]
        assert plan.bank_accounts[0].row["label"] == "Main"
    _reset()


def test_v2_account_number_iban_canonicalizes_and_reuses(monkeypatch, tmp_path):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import SubjectBankAccount
        from fakturek.native_backup_v2 import (
            _normalise_bank_account,
            build_native_backup_v2_import_plan,
        )

        row = _normalise_bank_account(
            {
                "label": "Main",
                "account_number": IBAN,
                "iban": "",
                "bic": "GIBACZPX",
                "country": "CZ",
                "currency": "CZK",
                "is_default": True,
                "sort_order": 0,
            }
        )
        assert row["account_number"] == "" and row["iban"] == IBAN
        db.add(
            SubjectBankAccount(
                subject_id=2,
                label="Existing",
                account_number="",
                iban=IBAN,
                country="CZ",
                currency="CZK",
                sort_order=5,
            )
        )
        db.commit()
        payload = _archive_with_account(row)
        run = _run(db, root, payload)
        plan = build_native_backup_v2_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert plan.bank_accounts[0].action == "reuse"
    _reset()


@pytest.mark.parametrize("field, value", [("country", "ČZ"), ("currency", "ČZK")])
def test_v2_rejects_non_ascii_country_and_currency(field, value):
    from fakturek.native_backup_v2 import _normalise_bank_account

    row = {
        "label": "A",
        "account_number": "1/0800",
        "iban": "",
        "bic": "",
        "country": "CZ",
        "currency": "CZK",
        "is_default": False,
        "sort_order": 0,
    }
    row[field] = value
    with pytest.raises(ValueError):
        _normalise_bank_account(row)


def test_v2_new_account_order_is_relative_and_existing_rows_are_untouched(
    monkeypatch, tmp_path
):
    SessionLocal, root = _setup(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import SubjectBankAccount
        from fakturek.native_backup_v2 import (
            build_native_backup_v2_bytes,
            process_native_backup_v2_import,
        )

        db.add(
            SubjectBankAccount(
                subject_id=1,
                label="First source",
                account_number="456/0800",
                country="CZ",
                currency="CZK",
                is_default=False,
                sort_order=1,
            )
        )
        db.add(
            SubjectBankAccount(
                subject_id=2,
                label="Existing destination",
                account_number="111/0800",
                country="CZ",
                currency="CZK",
                is_default=False,
                sort_order=20,
            )
        )
        db.commit()
        source_accounts = list(
            db.query(SubjectBankAccount)
            .filter_by(subject_id=1)
            .order_by(SubjectBankAccount.id)
        )
        payload = build_native_backup_v2_bytes(
            contacts=[],
            catalog_items=[],
            bank_accounts=source_accounts,
            max_member_bytes=10_000_000,
            max_archive_bytes=10_000_000,
        )
        run = _run(db, root, payload)
        process_native_backup_v2_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.commit()
        accounts = list(
            db.query(SubjectBankAccount)
            .filter_by(subject_id=2)
            .order_by(SubjectBankAccount.sort_order)
        )
        assert [(row.label, row.sort_order) for row in accounts] == [
            ("Existing destination", 20),
            ("First source", 21),
            ("Main", 22),
        ]
    _reset()

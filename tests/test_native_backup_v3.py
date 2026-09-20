from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module  # noqa: E402
from fakturek.db import Base  # noqa: E402
from fakturek.settings import get_settings  # noqa: E402

IBAN = "CZ6508000000192000145399"
YEAR = date.today().year


def _reset() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup(monkeypatch, tmp_path, *, auth: bool = False, csrf: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'v3.sqlite3'}")
    monkeypatch.setenv("IMPORT_STORAGE_DIR", str(tmp_path / "imports"))
    monkeypatch.setenv("AUTH_REQUIRED", "1" if auth else "0")
    monkeypatch.setenv("CSRF_ENABLED", "1" if csrf else "0")
    monkeypatch.setenv("SECRET_KEY", "v3-test-secret")
    _reset()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import (
        Contact,
        Invoice,
        InvoiceCatalogItem,
        InvoiceSeries,
        Subject,
        SubjectBankAccount,
    )

    Base.metadata.create_all(get_engine())
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(id=1, name="Source"),
                Subject(id=2, name="Destination"),
                Subject(id=3, name="Other destination"),
            ]
        )
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
        db.add(Contact(subject_id=2, name="Observed client", email="observed@example.test"))
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
        db.add(
            SubjectBankAccount(
                subject_id=1,
                label="Source account",
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
        db.add_all(
            [
                InvoiceSeries(
                    subject_id=1,
                    name="Default",
                    prefix="SRC",
                    pad_length=4,
                    last_counter=12,
                    last_counter_year=YEAR,
                ),
                InvoiceSeries(
                    subject_id=1,
                    name="Project",
                    prefix="PRJ",
                    pad_length=3,
                    last_counter=2,
                    last_counter_year=None,
                ),
                InvoiceSeries(
                    subject_id=2,
                    name="default",
                    prefix="ALT",
                    pad_length=3,
                    last_counter=7,
                    last_counter_year=YEAR,
                ),
            ]
        )
        db.flush()
        default_series_id = int(
            db.query(InvoiceSeries).filter_by(subject_id=2, name="default").one().id
        )
        destination_contact_id = int(
            db.query(Contact).filter_by(subject_id=2, name="Observed client").one().id
        )
        db.add(
            Invoice(
                subject_id=2,
                contact_id=destination_contact_id,
                series_id=default_series_id,
                number=f"{YEAR}-ALT-009",
                status="issued",
                issue_date=date(YEAR, 1, 3),
                due_date=date(YEAR, 1, 17),
                currency="CZK",
                total_cents=0,
                buyer_name_cache="Observed client",
            )
        )
        db.add(
            SubjectBankAccount(
                subject_id=2,
                label="Destination account",
                account_number="different",
                iban=IBAN,
                bic="DIFFERENT",
                country="CZ",
                currency="CZK",
                is_default=True,
                sort_order=77,
                fio_api_token="destination-secret",
            )
        )
        if auth:
            from fakturek.auth import hash_password
            from fakturek.models import User, UserSubject

            db.add_all(
                [
                    User(
                        id=1,
                        username="owner",
                        email="owner@example.test",
                        password_hash=hash_password("secret123", iterations=1_000),
                        is_active=True,
                    ),
                    User(
                        id=2,
                        username="viewer",
                        email="viewer@example.test",
                        password_hash=hash_password("secret123", iterations=1_000),
                        is_active=True,
                    ),
                    UserSubject(
                        user_id=1,
                        subject_id=1,
                        role="owner",
                        can_view=True,
                        can_edit=True,
                        can_issue=True,
                        can_export=True,
                    ),
                    UserSubject(
                        user_id=2,
                        subject_id=1,
                        role="viewer",
                        can_view=True,
                        can_edit=False,
                        can_issue=False,
                        can_export=False,
                    ),
                ]
            )
        db.commit()
    return TestClient(create_app()), SessionLocal, tmp_path / "imports"


def _login(client: TestClient, identifier: str) -> None:
    response = client.post(
        "/login",
        data={
            "identifier": identifier,
            "password": "secret123",  # pragma: allowlist secret
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _csrf(client: TestClient) -> str:
    response = client.get("/imports")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _payload(SessionLocal) -> bytes:
    from fakturek.models import Contact, InvoiceCatalogItem, InvoiceSeries, SubjectBankAccount
    from fakturek.native_backup_v3 import build_native_backup_v3_bytes

    with SessionLocal() as db:
        return build_native_backup_v3_bytes(
            contacts=list(db.query(Contact).filter_by(subject_id=1)),
            catalog_items=list(db.query(InvoiceCatalogItem).filter_by(subject_id=1)),
            bank_accounts=list(db.query(SubjectBankAccount).filter_by(subject_id=1)),
            invoice_series=list(db.query(InvoiceSeries).filter_by(subject_id=1)),
            max_member_bytes=10_000_000,
            max_archive_bytes=10_000_000,
        )


def _run(db, root: Path, payload: bytes, *, subject_id: int = 2):
    from fakturek.models import ImportRun

    run = ImportRun(
        subject_id=subject_id,
        source="fakturek_native_v3",
        status="uploaded",
        file_name="native-backup-v3.zip",
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


def _series_archive(
    rows: list[dict],
    *,
    omit_member: str | None = None,
    extra_member: str | None = None,
    checksum: str | None = None,
    version: int = 3,
) -> bytes:
    contacts = b""
    catalog = b""
    accounts = b""
    series = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    entries = [
        ("contacts", "contacts.jsonl", contacts),
        ("catalog_items", "catalog_items.jsonl", catalog),
        ("bank_accounts", "bank_accounts.jsonl", accounts),
        ("invoice_series", "invoice_series.jsonl", series),
    ]
    manifest = {
        "format": "fakturek-native-backup",
        "version": version,
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "export_id": str(uuid4()),
        "datasets": [
            {
                "name": name,
                "filename": filename,
                "schema_version": 1,
                "row_count": data.count(b"\n"),
                "sha256": (
                    checksum
                    if name == "invoice_series" and checksum is not None
                    else hashlib.sha256(data).hexdigest()
                ),
            }
            for name, filename, data in entries
        ],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        if omit_member != "manifest.json":
            archive.writestr("manifest.json", json.dumps(manifest).encode())
        for _name, filename, data in entries:
            if filename != omit_member:
                archive.writestr(filename, data)
        if extra_member is not None:
            archive.writestr(extra_member, b"x")
    return out.getvalue()


def _valid_series_row(**overrides) -> dict:
    row = {
        "name": "One",
        "prefix": "ONE",
        "pad_length": 4,
        "last_counter": 0,
        "last_counter_year": None,
    }
    row.update(overrides)
    return row


def _rewrite_manifest(payload: bytes, mutate) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    mutate(manifest)
    members["manifest.json"] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_v3_route_export_has_exact_members_fields_and_exclusions(monkeypatch, tmp_path):
    client, SessionLocal, _root = _setup(monkeypatch, tmp_path)

    response = client.get("/exports/native-backup-v3.zip")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.fakturek.native-backup-v3+zip"
    )
    assert "fakturek-native-backup-v3.zip" in response.headers["content-disposition"]

    second = _payload(SessionLocal)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "contacts.jsonl",
            "catalog_items.jsonl",
            "bank_accounts.jsonl",
            "invoice_series.jsonl",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "fakturek-native-backup"
        assert manifest["version"] == 3
        assert [row["name"] for row in manifest["datasets"]] == [
            "contacts",
            "catalog_items",
            "bank_accounts",
            "invoice_series",
        ]
        second_manifest = json.loads(zipfile.ZipFile(io.BytesIO(second)).read("manifest.json"))
        assert manifest["export_id"] != second_manifest["export_id"]
        for dataset in manifest["datasets"]:
            content = archive.read(dataset["filename"])
            assert dataset["schema_version"] == 1
            assert dataset["row_count"] == content.count(b"\n")
            assert dataset["sha256"] == hashlib.sha256(content).hexdigest()

        allowed = {
            "contacts.jsonl": {
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
            },
            "catalog_items.jsonl": {
                "description",
                "quantity",
                "unit",
                "unit_price_cents",
                "vat_rate",
                "currency",
            },
            "bank_accounts.jsonl": {
                "label",
                "account_number",
                "iban",
                "bic",
                "country",
                "currency",
                "is_default",
                "sort_order",
            },
            "invoice_series.jsonl": {
                "name",
                "prefix",
                "pad_length",
                "last_counter",
                "last_counter_year",
            },
        }
        forbidden = (
            "super-secret-token",
            "destination-secret",
            "never export",
            "fio_api",
            '"999"',
            "payment_sync",
            "registry_last_error",
            "registry_data_hash",
            "recurring",
            "profile",
            "settings",
            "transactions",
        )
        for filename, exact_keys in allowed.items():
            rows = [json.loads(line) for line in archive.read(filename).splitlines()]
            assert rows
            for row in rows:
                assert set(row) == exact_keys
                serialized = json.dumps(row, ensure_ascii=False)
                assert all(value not in serialized for value in forbidden)
    _reset()


def test_v3_roundtrip_reuses_existing_series_unchanged_and_is_idempotent(monkeypatch, tmp_path):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)

    with SessionLocal() as db:
        from fakturek.models import (
            Contact,
            ImportMap,
            InvoiceCatalogItem,
            InvoiceSeries,
            SubjectBankAccount,
        )
        from fakturek.native_backup_v3 import (
            build_native_backup_v3_import_plan,
            preview_native_backup_v3_import,
            process_native_backup_v3_import,
        )

        run = _run(db, root, payload)
        plan = build_native_backup_v3_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert [action.action for action in plan.invoice_series] == ["reuse", "create"]
        preview = preview_native_backup_v3_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
            plan=plan,
        )
        assert preview["source"] == "fakturek_native_v3"
        assert preview["contacts"] == {"parsed": 1, "will_create": 1, "will_reuse": 0}
        assert preview["catalog_items"] == {"parsed": 1, "will_create": 1, "will_reuse": 0}
        assert preview["bank_accounts"]["will_reuse"] == 1
        assert preview["invoice_series"]["will_create"] == 1
        assert preview["invoice_series"]["will_reuse"] == 1
        assert preview["invoice_series"]["rows"] == [
            {
                "name": "default",
                "action": "reuse",
                "next_number": f"{YEAR}-ALT-010",
            },
            {
                "name": "Project",
                "action": "create",
                "next_number": f"{YEAR}-PRJ-001",
            },
        ]

        process_native_backup_v3_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
            plan=plan,
        )
        db.commit()
        process_native_backup_v3_import(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.commit()

        assert db.query(Contact).filter_by(subject_id=2).count() == 2
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=2).count() == 1
        assert db.query(SubjectBankAccount).filter_by(subject_id=2).count() == 1
        account = db.query(SubjectBankAccount).filter_by(subject_id=2).one()
        assert account.label == "Destination account"
        assert account.account_number == "different"
        assert account.fio_api_token == "destination-secret"

        existing = db.query(InvoiceSeries).filter_by(subject_id=2, name="default").one()
        assert existing.prefix == "ALT"
        assert existing.pad_length == 3
        assert existing.last_counter == 7
        created = db.query(InvoiceSeries).filter_by(subject_id=2, name="Project").one()
        assert created.prefix == "PRJ"
        assert created.pad_length == 3
        assert created.last_counter == 2
        assert created.last_counter_year is None
        assert db.query(ImportMap).filter_by(subject_id=2, source="fakturek_native_v3").count() == 5
    _reset()


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda: _series_archive([_valid_series_row()], omit_member="invoice_series.jsonl"),
        lambda: _series_archive([_valid_series_row()], extra_member="surprise.txt"),
        lambda: _series_archive([_valid_series_row()], extra_member="../bad"),
        lambda: _series_archive([_valid_series_row()], checksum="0" * 64),
        lambda: _series_archive([{k: v for k, v in _valid_series_row().items() if k != "prefix"}]),
        lambda: _series_archive([_valid_series_row(extra="no")]),
        lambda: _series_archive([_valid_series_row(pad_length=True)]),
        lambda: _series_archive([_valid_series_row(pad_length=0)]),
        lambda: _series_archive([_valid_series_row(pad_length=21)]),
        lambda: _series_archive([_valid_series_row(last_counter=-1)]),
        lambda: _series_archive([_valid_series_row(last_counter=2_147_483_648)]),
        lambda: _series_archive([_valid_series_row(last_counter_year=True)]),
        lambda: _series_archive([_valid_series_row(last_counter_year=1899)]),
        lambda: _series_archive([_valid_series_row(last_counter_year=10000)]),
        lambda: _series_archive([_valid_series_row(name="One"), _valid_series_row(name=" one ")]),
        lambda: _series_archive([_valid_series_row()], version=2),
    ],
)
def test_v3_rejects_malformed_extra_missing_type_bounds_duplicate_and_checksum(
    payload_factory,
):
    from fakturek.native_backup_v3 import parse_native_backup_v3_bytes

    with pytest.raises(ValueError):
        parse_native_backup_v3_bytes(
            payload_factory(),
            max_member_bytes=10_000_000,
            max_total_bytes=10_000_000,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.__setitem__("format", True),
        lambda manifest: manifest.__setitem__("version", 3.0),
        lambda manifest: manifest.__setitem__("generated_at_utc", 123),
        lambda manifest: manifest.__setitem__("export_id", ["not", "text"]),
        lambda manifest: manifest["datasets"][0].__setitem__("name", ["contacts"]),
        lambda manifest: manifest["datasets"][0].__setitem__("filename", 7),
        lambda manifest: manifest["datasets"][0].__setitem__("schema_version", True),
        lambda manifest: manifest["datasets"][0].__setitem__("row_count", 0.0),
        lambda manifest: manifest["datasets"][0].__setitem__("sha256", ["0" * 64]),
    ],
)
def test_v3_manifest_requires_exact_scalar_types_and_normalizes_errors(mutate):
    from fakturek.native_backup_v3 import parse_native_backup_v3_bytes

    payload = _rewrite_manifest(_series_archive([_valid_series_row()]), mutate)
    with pytest.raises(ValueError):
        parse_native_backup_v3_bytes(
            payload,
            max_member_bytes=10_000_000,
            max_total_bytes=10_000_000,
        )


def test_v3_rejects_archive_member_upload_and_path_bounds(monkeypatch, tmp_path):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _series_archive([_valid_series_row()])

    from fakturek.native_backup_v3 import (
        build_native_backup_v3_import_plan,
        parse_native_backup_v3_bytes,
    )

    with pytest.raises(ValueError):
        parse_native_backup_v3_bytes(payload, max_member_bytes=10, max_total_bytes=10_000_000)
    with pytest.raises(ValueError):
        parse_native_backup_v3_bytes(payload, max_member_bytes=10_000_000, max_total_bytes=10)
    with pytest.raises(ValueError):
        parse_native_backup_v3_bytes(b"", max_member_bytes=10_000_000, max_total_bytes=10_000_000)

    with SessionLocal() as db:
        from fakturek.models import ImportRun, InvoiceSeries

        run = ImportRun(
            subject_id=2,
            source="fakturek_native_v3",
            status="uploaded",
            file_name="escape.zip",
            file_sha256=hashlib.sha256(payload).hexdigest(),
            file_size_bytes=len(payload),
            mime_type="application/zip",
            file_path="../escape.zip",
        )
        db.add(run)
        db.flush()
        with pytest.raises(ValueError):
            build_native_backup_v3_import_plan(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )
        assert db.query(InvoiceSeries).filter_by(subject_id=2, name="One").count() == 0
    _reset()


def test_v3_target_case_collision_and_stale_plan_are_rejected_before_writes(monkeypatch, tmp_path):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)

    with SessionLocal() as db:
        from fakturek.models import Contact, InvoiceSeries
        from fakturek.native_backup_v3 import (
            build_native_backup_v3_import_plan,
            process_native_backup_v3_import,
        )

        db.add_all(
            [
                InvoiceSeries(
                    subject_id=3,
                    name="Case",
                    prefix="A",
                    pad_length=4,
                    last_counter=0,
                ),
                InvoiceSeries(
                    subject_id=3,
                    name="case",
                    prefix="B",
                    pad_length=4,
                    last_counter=0,
                ),
            ]
        )
        db.commit()
        with pytest.raises(ValueError, match="duplicate normalized invoice series"):
            build_native_backup_v3_import_plan(
                db,
                run=_run(db, root, payload, subject_id=3),
                subject_id=3,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )

        run = _run(db, root, payload)
        plan = build_native_backup_v3_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        series = db.query(InvoiceSeries).filter_by(subject_id=2, name="default").one()
        series.last_counter = 8
        db.commit()
        with pytest.raises(ValueError, match="changed since preview"):
            process_native_backup_v3_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
                plan=plan,
            )
        db.rollback()
        assert db.query(Contact).filter_by(subject_id=2).count() == 1
        assert db.query(InvoiceSeries).filter_by(subject_id=2, name="Project").count() == 0
    _reset()


@pytest.mark.parametrize("entity_type", ["contact", "catalog_item"])
def test_v3_stale_contact_and_catalog_identities_fail_atomically(
    monkeypatch, tmp_path, entity_type
):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    if entity_type == "contact":
        with SessionLocal() as db:
            from fakturek.models import Contact

            source = db.query(Contact).filter_by(subject_id=1).one()
            source.external_source = None
            source.external_id = None
            source.ico = None
            db.commit()
    payload = _payload(SessionLocal)

    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, InvoiceCatalogItem, InvoiceSeries
        from fakturek.native_backup_v3 import (
            build_native_backup_v3_import_plan,
            process_native_backup_v3_import,
        )

        if entity_type == "contact":
            target = Contact(
                subject_id=2,
                name="Native customer",
                email="customer@example.test",
                external_source="destination-crm",
                external_id="destination-contact",
            )
        else:
            target = InvoiceCatalogItem(
                subject_id=2,
                description="Native service",
                quantity=Decimal("2.00"),
                unit="hour",
                unit_price_cents=12345,
                vat_rate=Decimal("21.00"),
                currency="CZK",
            )
        db.add(target)
        db.commit()

        run = _run(db, root, payload)
        plan = build_native_backup_v3_import_plan(
            db,
            run=run,
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        actions = plan.contacts if entity_type == "contact" else plan.catalog_items
        assert actions[0].action == "reuse"

        if entity_type == "contact":
            target.email = "changed-after-preview@example.test"
        else:
            target.unit_price_cents = 99999
        db.commit()

        with pytest.raises(ValueError, match="destination changed since preview"):
            process_native_backup_v3_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
                plan=plan,
            )
        db.rollback()
        assert db.query(ImportMap).filter_by(subject_id=2, source="fakturek_native_v3").count() == 0
        assert db.query(InvoiceSeries).filter_by(subject_id=2, name="Project").count() == 0
    _reset()


def test_v3_counter_boundary_accepts_last_issuable_and_marks_exhausted_observed_counter(
    monkeypatch, tmp_path
):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    from fakturek.models import Contact, Invoice, InvoiceSeries
    from fakturek.native_backup_v3 import build_native_backup_v3_import_plan

    valid_payload = _series_archive(
        [
            _valid_series_row(
                prefix="BOUND",
                last_counter=2_147_483_646,
                last_counter_year=YEAR,
            )
        ]
    )
    with SessionLocal() as db:
        valid_plan = build_native_backup_v3_import_plan(
            db,
            run=_run(db, root, valid_payload, subject_id=3),
            subject_id=3,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert valid_plan.series_preview[0]["next_number"].endswith("2147483647")

        contact_id = int(db.query(Contact).filter_by(subject_id=2).one().id)
        series_id = int(db.query(InvoiceSeries).filter_by(subject_id=2, name="default").one().id)
        db.add(
            Invoice(
                subject_id=2,
                contact_id=contact_id,
                series_id=series_id,
                number=f"{YEAR}-BOUND-2147483647",
                status="issued",
                issue_date=date(YEAR, 1, 4),
                due_date=date(YEAR, 1, 18),
                currency="CZK",
                total_cents=0,
                buyer_name_cache="Observed client",
            )
        )
        db.commit()
        exhausted_payload = _series_archive([_valid_series_row(prefix="BOUND")])
        exhausted_plan = build_native_backup_v3_import_plan(
            db,
            run=_run(db, root, exhausted_payload),
            subject_id=2,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        assert exhausted_plan.series_warnings
        assert exhausted_plan.series_preview[0]["next_number"] is None
        assert exhausted_plan.series_preview[0]["exhausted"] is True
    _reset()


@pytest.mark.parametrize("distinct_prefixes", [False, True])
def test_v3_preview_loads_invoices_once_for_shared_and_distinct_prefixes(
    monkeypatch, tmp_path, distinct_prefixes
):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    from sqlalchemy import event

    from fakturek.native_backup_v3 import build_native_backup_v3_import_plan

    payload = _series_archive(
        [
            _valid_series_row(
                name=f"Series {index}",
                prefix=f"PREFIX-{index}" if distinct_prefixes else "SHARED",
            )
            for index in range(1000)
        ]
    )
    invoice_queries = 0

    def count_invoice_queries(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal invoice_queries
        if "FROM invoices" in statement and "invoices.number" in statement:
            invoice_queries += 1

    with SessionLocal() as db:
        event.listen(db.get_bind(), "before_cursor_execute", count_invoice_queries)
        try:
            plan = build_native_backup_v3_import_plan(
                db,
                run=_run(db, root, payload, subject_id=3),
                subject_id=3,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", count_invoice_queries)
        assert len(plan.invoice_series) == 1000
        assert all(action.action == "create" for action in plan.invoice_series)
        assert invoice_queries == 1
    _reset()


def test_v3_rejects_accent_insensitive_series_collisions_without_overwrite(monkeypatch, tmp_path):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    from fakturek.models import InvoiceSeries
    from fakturek.native_backup_v3 import (
        build_native_backup_v3_import_plan,
        parse_native_backup_v3_bytes,
    )

    with pytest.raises(ValueError, match="colliding normalized invoice series"):
        parse_native_backup_v3_bytes(
            _series_archive([_valid_series_row(name="Rada"), _valid_series_row(name="Řada")]),
            max_member_bytes=10_000_000,
            max_total_bytes=10_000_000,
        )
    with pytest.raises(ValueError, match="colliding normalized invoice series"):
        parse_native_backup_v3_bytes(
            _series_archive([_valid_series_row(name="AB"), _valid_series_row(name="A\u200dB")]),
            max_member_bytes=10_000_000,
            max_total_bytes=10_000_000,
        )

    with SessionLocal() as db:
        existing = InvoiceSeries(
            subject_id=3,
            name="Rada",
            prefix="SAFE",
            pad_length=4,
            last_counter=17,
        )
        db.add(existing)
        db.commit()
        with pytest.raises(ValueError, match="database collation"):
            build_native_backup_v3_import_plan(
                db,
                run=_run(
                    db,
                    root,
                    _series_archive([_valid_series_row(name="Řada", prefix="DANGER")]),
                    subject_id=3,
                ),
                subject_id=3,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )
        db.refresh(existing)
        assert existing.prefix == "SAFE"
        assert existing.last_counter == 17
        assert db.query(InvoiceSeries).filter_by(subject_id=3).count() == 1
    _reset()


def test_v3_finished_upload_can_be_reapplied_after_master_data_deletion(monkeypatch, tmp_path):
    client, SessionLocal, _root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)

    first_upload = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup-v3.zip", payload, "application/zip")},
        follow_redirects=False,
    )
    assert first_upload.status_code == 303
    first_run_id = int(first_upload.headers["location"].split("/")[2].split("?")[0])
    active_duplicate = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup-v3.zip", payload, "application/zip")},
        follow_redirects=False,
    )
    assert active_duplicate.status_code == 303
    assert active_duplicate.headers["location"] == f"/imports/{first_run_id}?duplicate=1"
    first_process = client.post(f"/imports/{first_run_id}/process", follow_redirects=False)
    assert first_process.status_code == 303
    assert first_process.headers["location"] == f"/imports/{first_run_id}?processed=1"

    with SessionLocal() as db:
        from fakturek.models import Contact, InvoiceCatalogItem

        db.query(Contact).filter_by(subject_id=1).delete(synchronize_session=False)
        db.query(InvoiceCatalogItem).filter_by(subject_id=1).delete(synchronize_session=False)
        db.commit()

    second_upload = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup-v3.zip", payload, "application/zip")},
        follow_redirects=False,
    )
    assert second_upload.status_code == 303
    second_run_id = int(second_upload.headers["location"].split("/")[2].split("?")[0])
    assert second_run_id != first_run_id
    second_process = client.post(f"/imports/{second_run_id}/process", follow_redirects=False)
    assert second_process.status_code == 303
    assert second_process.headers["location"] == f"/imports/{second_run_id}?processed=1"

    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun, InvoiceCatalogItem

        assert db.get(ImportRun, first_run_id).status == "finished"
        assert db.get(ImportRun, second_run_id).status == "finished"
        assert db.query(Contact).filter_by(subject_id=1, name="Native customer").count() == 1
        assert (
            db.query(InvoiceCatalogItem)
            .filter_by(subject_id=1, description="Native service")
            .count()
            == 1
        )
        maps = db.query(ImportMap).filter_by(subject_id=1, source="fakturek_native_v3")
        assert maps.count() == 5
    _reset()


def test_v3_finished_upload_retargets_orphan_maps_to_manual_replacements(monkeypatch, tmp_path):
    client, SessionLocal, _root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)

    first_upload = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup-v3.zip", payload, "application/zip")},
        follow_redirects=False,
    )
    first_run_id = int(first_upload.headers["location"].split("/")[2].split("?")[0])
    assert (
        client.post(f"/imports/{first_run_id}/process", follow_redirects=False).status_code == 303
    )

    with SessionLocal() as db:
        from fakturek.models import Contact, InvoiceCatalogItem

        original_contact = db.query(Contact).filter_by(subject_id=1).one()
        original_catalog = db.query(InvoiceCatalogItem).filter_by(subject_id=1).one()
        original_contact_id = int(original_contact.id)
        original_catalog_id = int(original_catalog.id)
        # Keep SQLite from reusing the deleted catalog row id, matching the
        # monotonic auto-increment behavior used in MariaDB production.
        db.add(
            InvoiceCatalogItem(
                subject_id=2,
                description="id sentinel",
                quantity=Decimal("1.00"),
                unit="piece",
                unit_price_cents=1,
                vat_rate=Decimal("0.00"),
                currency="CZK",
            )
        )
        db.flush()
        db.delete(original_contact)
        db.delete(original_catalog)
        db.flush()
        manual_contact = Contact(
            subject_id=1,
            name="Native customer",
            external_source="crm",
            external_id="customer-7",
        )
        manual_catalog = InvoiceCatalogItem(
            subject_id=1,
            description="Native service",
            quantity=Decimal("2.00"),
            unit="hour",
            unit_price_cents=12345,
            vat_rate=Decimal("21.00"),
            currency="CZK",
        )
        db.add_all([manual_contact, manual_catalog])
        db.commit()
        manual_contact_id = int(manual_contact.id)
        manual_catalog_id = int(manual_catalog.id)
        assert manual_contact_id != original_contact_id
        assert manual_catalog_id != original_catalog_id

    second_upload = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup-v3.zip", payload, "application/zip")},
        follow_redirects=False,
    )
    second_run_id = int(second_upload.headers["location"].split("/")[2].split("?")[0])
    second_process = client.post(f"/imports/{second_run_id}/process", follow_redirects=False)
    assert second_process.status_code == 303
    assert second_process.headers["location"] == f"/imports/{second_run_id}?processed=1"

    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun, InvoiceCatalogItem

        assert db.get(ImportRun, second_run_id).status == "finished"
        assert db.query(Contact).filter_by(subject_id=1).count() == 1
        assert (
            db.query(InvoiceCatalogItem)
            .filter_by(subject_id=1, description="Native service")
            .count()
            == 1
        )
        contact_map = (
            db.query(ImportMap)
            .filter_by(
                subject_id=1,
                source="fakturek_native_v3",
                entity_type="contact",
            )
            .one()
        )
        catalog_map = (
            db.query(ImportMap)
            .filter_by(
                subject_id=1,
                source="fakturek_native_v3",
                entity_type="catalog_item",
            )
            .one()
        )
        assert int(contact_map.internal_id) == manual_contact_id
        assert int(catalog_map.internal_id) == manual_catalog_id
        assert db.query(ImportMap).filter_by(subject_id=1, source="fakturek_native_v3").count() == 5
    _reset()


def test_v3_exhausted_counter_warns_but_restores_and_reuses_without_overwrite(
    monkeypatch, tmp_path
):
    client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import InvoiceSeries

        exhausted = db.query(InvoiceSeries).filter_by(subject_id=1, name="Default").one()
        exhausted.last_counter = 2_147_483_647
        exhausted.last_counter_year = YEAR
        db.commit()

    exported = client.get("/exports/native-backup-v3.zip")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        rows = [json.loads(line) for line in archive.read("invoice_series.jsonl").splitlines()]
    exhausted_row = next(row for row in rows if row["name"] == "Default")
    assert exhausted_row["last_counter"] == 2_147_483_647

    with SessionLocal() as db:
        from fakturek.models import InvoiceSeries

        # A name match is reuse-only: restoring the archive must not overwrite
        # destination formatting, even when the destination counter is exhausted.
        existing = db.query(InvoiceSeries).filter_by(subject_id=1, name="Default").one()
        existing.prefix = "LIVE"
        existing.pad_length = 9
        db.commit()

    uploaded = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("exhausted-v3.zip", exported.content, "application/zip")},
        follow_redirects=False,
    )
    run_id = int(uploaded.headers["location"].split("/")[2].split("?")[0])
    detail = client.get(uploaded.headers["location"])
    assert "Vyčerpáno — další číslo nelze přidělit" in detail.text
    assert "Uložený stav se přesto bezpečně obnoví" in detail.text
    assert "Import nelze spustit" not in detail.text
    assert "Spustit import" in detail.text

    processed = client.post(f"/imports/{run_id}/process", follow_redirects=False)
    assert processed.status_code == 303
    assert processed.headers["location"] == f"/imports/{run_id}?processed=1"
    with SessionLocal() as db:
        from fakturek.models import Contact, ImportMap, ImportRun, Invoice, InvoiceSeries

        assert db.get(ImportRun, run_id).status == "finished"
        existing = db.query(InvoiceSeries).filter_by(subject_id=1, name="Default").one()
        assert existing.prefix == "LIVE"
        assert existing.pad_length == 9
        assert existing.last_counter == 2_147_483_647
        assert db.query(ImportMap).filter_by(subject_id=1, source="fakturek_native_v3").count() == 5
        contact = db.query(Contact).filter_by(subject_id=1).one()
        draft = Invoice(
            subject_id=1,
            contact_id=int(contact.id),
            series_id=int(existing.id),
            number="DRAFT-EXHAUSTED",
            status="draft",
            issue_date=date(YEAR, 1, 5),
            due_date=date(YEAR, 1, 19),
            currency="CZK",
            total_cents=0,
            buyer_name_cache=str(contact.name),
        )
        db.add(draft)
        db.commit()
        draft_id = int(draft.id)

    issue = client.post(f"/invoices/{draft_id}/issue", follow_redirects=False)
    assert issue.status_code == 409
    assert "další číslo nelze přidělit" in issue.text
    with SessionLocal() as db:
        from fakturek.models import Invoice, InvoiceSeries

        unchanged_draft = db.get(Invoice, draft_id)
        unchanged_series = db.query(InvoiceSeries).filter_by(subject_id=1, name="Default").one()
        assert unchanged_draft.status == "draft"
        assert unchanged_draft.number == "DRAFT-EXHAUSTED"
        assert unchanged_series.last_counter == 2_147_483_647
        assert unchanged_series.last_counter_year == YEAR

    # The same valid archive can also restore all unrelated master data and
    # create the exhausted state in an empty destination atomically.
    with SessionLocal() as db:
        from fakturek.models import (
            Contact,
            ImportMap,
            InvoiceCatalogItem,
            InvoiceSeries,
            SubjectBankAccount,
        )
        from fakturek.native_backup_v3 import process_native_backup_v3_import

        restore_run = _run(db, root, exported.content, subject_id=3)
        summary = process_native_backup_v3_import(
            db,
            run=restore_run,
            subject_id=3,
            import_storage_root=root,
            max_upload_bytes=10_000_000,
        )
        db.commit()
        assert summary["contacts"]["created"] == 1
        assert summary["catalog_items"]["created"] == 1
        assert summary["bank_accounts"]["created"] == 1
        assert summary["invoice_series"]["created"] == 2
        assert db.query(Contact).filter_by(subject_id=3).count() == 1
        assert db.query(InvoiceCatalogItem).filter_by(subject_id=3).count() == 1
        assert db.query(SubjectBankAccount).filter_by(subject_id=3).count() == 1
        restored = db.query(InvoiceSeries).filter_by(subject_id=3, name="Default").one()
        assert restored.prefix == "SRC"
        assert restored.pad_length == 4
        assert restored.last_counter == 2_147_483_647
        assert restored.last_counter_year == YEAR
        assert db.query(ImportMap).filter_by(subject_id=3, source="fakturek_native_v3").count() == 5
    _reset()


def test_v3_cross_tenant_run_rejects_without_writes(monkeypatch, tmp_path):
    _client, SessionLocal, root = _setup(monkeypatch, tmp_path)
    payload = _payload(SessionLocal)
    with SessionLocal() as db:
        from fakturek.models import Contact, InvoiceSeries
        from fakturek.native_backup_v3 import process_native_backup_v3_import

        run = _run(db, root, payload, subject_id=3)
        with pytest.raises(ValueError, match="current subject"):
            process_native_backup_v3_import(
                db,
                run=run,
                subject_id=2,
                import_storage_root=root,
                max_upload_bytes=10_000_000,
            )
        assert db.query(Contact).filter_by(subject_id=2).count() == 1
        assert db.query(InvoiceSeries).filter_by(subject_id=2, name="Project").count() == 0
    _reset()


def test_v3_invalid_preview_hides_process_and_direct_post_fails_closed(monkeypatch, tmp_path):
    client, SessionLocal, _root = _setup(monkeypatch, tmp_path)
    payload = _series_archive([_valid_series_row(fio_api_token="must-not-import")])

    uploaded = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("bad-v3.zip", payload, "application/zip")},
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
        from fakturek.models import ImportRun, InvoiceSeries

        assert db.get(ImportRun, run_id).status == "error"
        assert db.query(InvoiceSeries).filter_by(subject_id=1, name="One").count() == 0
    retry_detail = client.get(f"/imports/{run_id}")
    assert "Import nelze spustit" in retry_detail.text
    assert "Spustit import" not in retry_detail.text
    _reset()


def test_v3_upload_csrf_detail_ui_and_export_permission(monkeypatch, tmp_path):
    client, _SessionLocal, _root = _setup(monkeypatch, tmp_path, csrf=True)
    page = client.get("/imports")
    assert "Fakturek native backup v3" in page.text
    assert "/exports/native-backup-v3.zip" in page.text

    exported = client.get("/exports/native-backup-v3.zip")
    assert exported.status_code == 200
    denied = client.post(
        "/imports",
        data={"source": "fakturek_native_v3"},
        files={"file": ("backup.zip", exported.content, "application/zip")},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    accepted = client.post(
        "/imports",
        data={"source": "fakturek_native_v3", "csrf_token": _csrf(client)},
        files={"file": ("backup.zip", exported.content, "application/zip")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    detail = client.get(accepted.headers["location"])
    assert "Native backup v3" in detail.text
    assert "Řady checksum" in detail.text
    assert "Spustit import" in detail.text

    from fakturek.ui_i18n import translate_html_document

    english_detail = translate_html_document(detail.text, "en")
    assert "Invoice series" in english_detail
    assert "Series checksum" in english_detail
    assert "The preview is current but non-binding" in english_detail
    _reset()

    auth_client, _AuthSessionLocal, _auth_root = _setup(monkeypatch, tmp_path / "auth", auth=True)
    _login(auth_client, "viewer")
    forbidden = auth_client.get("/exports/native-backup-v3.zip")
    assert forbidden.status_code == 403
    _reset()


def test_v3_archives_remain_incompatible_with_v1_and_v2(monkeypatch, tmp_path):
    _client, SessionLocal, _root = _setup(monkeypatch, tmp_path)
    from fakturek.models import SubjectBankAccount
    from fakturek.native_backup import build_native_backup_bytes, parse_native_backup_bytes
    from fakturek.native_backup_v2 import build_native_backup_v2_bytes, parse_native_backup_v2_bytes
    from fakturek.native_backup_v3 import parse_native_backup_v3_bytes

    v1 = build_native_backup_bytes(
        contacts=[],
        catalog_items=[],
        max_member_bytes=10_000_000,
        max_archive_bytes=10_000_000,
    )
    with SessionLocal() as db:
        v2 = build_native_backup_v2_bytes(
            contacts=[],
            catalog_items=[],
            bank_accounts=list(db.query(SubjectBankAccount).filter_by(subject_id=1)),
            max_member_bytes=10_000_000,
            max_archive_bytes=10_000_000,
        )
    v3 = _payload(SessionLocal)

    for parser, payload in (
        (parse_native_backup_bytes, v2),
        (parse_native_backup_bytes, v3),
        (parse_native_backup_v2_bytes, v1),
        (parse_native_backup_v2_bytes, v3),
        (parse_native_backup_v3_bytes, v1),
        (parse_native_backup_v3_bytes, v2),
    ):
        with pytest.raises(ValueError):
            parser(payload, max_member_bytes=10_000_000, max_total_bytes=10_000_000)
    _reset()

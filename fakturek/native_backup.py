"""Strict Fakturek-native backup v1 for contacts and catalog items only.

The format deliberately contains no database identifiers.  Import identity is a
digest of each canonical business row and is scoped by ``ImportMap`` to the
destination subject.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

FORMAT = "fakturek-native-backup"
VERSION = 1
SOURCE = "fakturek_native_v1"
MEDIA_TYPE = "application/vnd.fakturek.native-backup+zip"
MEMBERS = ("manifest.json", "contacts.jsonl", "catalog_items.jsonl")
SCHEMA_VERSION = 1
MAX_ROWS_PER_DATASET = 100_000

CONTACT_FIELDS = (
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
)
CATALOG_FIELDS = ("description", "quantity", "unit", "unit_price_cents", "vat_rate", "currency")


@dataclass(frozen=True)
class NativeBackup:
    manifest: dict[str, Any]
    contacts: list[dict[str, Any]]
    catalog_items: list[dict[str, Any]]


@dataclass(frozen=True)
class NativeImportAction:
    identity: str
    row: dict[str, Any]
    action: str
    existing_id: int | None = None


@dataclass(frozen=True)
class NativeImportPlan:
    backup: NativeBackup
    contact_mode: str
    contacts: list[NativeImportAction]
    catalog_items: list[NativeImportAction]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(row) + b"\n" for row in rows)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def row_identity(row: dict[str, Any]) -> str:
    """Stable ImportMap external id for a v1 business row."""

    return _digest(_canonical_json(row))


def _clean_text(
    value: object | None, *, max_length: int, field: str, required: bool = False
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > max_length:
        raise ValueError(f"{field} is too long")
    return result or None


def _decimal_text(value: object, *, field: str, maximum: Decimal) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not decimal.is_finite() or decimal < Decimal("0") or decimal > maximum:
        raise ValueError(f"{field} is out of range")
    if decimal.as_tuple().exponent < -2:
        raise ValueError(f"{field} has too many decimal places")
    return format(decimal.quantize(Decimal("0.01")), "f")


def _normalise_contact(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("contact row must be an object")
    if set(raw) - set(CONTACT_FIELDS):
        raise ValueError("contact row has unsupported fields")
    if "name" not in raw:
        raise ValueError("contact row is missing name")
    field_limits = {
        "name": 255,
        "email": 255,
        "phone": 50,
        "street": 255,
        "city": 100,
        "zip": 20,
        "country": 2,
        "ico": 32,
        "dic": 32,
        "fixed_variable_symbol": 10,
        "external_source": 32,
        "external_id": 255,
    }
    out: dict[str, Any] = {
        "name": _clean_text(raw.get("name"), max_length=255, field="name", required=True)
    }
    for name, length in field_limits.items():
        if name == "name" or name not in raw:
            continue
        value = _clean_text(raw.get(name), max_length=length, field=name)
        if value is not None:
            out[name] = value.upper() if name == "country" else value
    if "registry_auto_update" in raw:
        if not isinstance(raw["registry_auto_update"], bool):
            raise ValueError("registry_auto_update must be a boolean")
        out["registry_auto_update"] = raw["registry_auto_update"]
    return out


def _normalise_catalog_item(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("catalog item row must be an object")
    if set(raw) != set(CATALOG_FIELDS):
        raise ValueError("catalog item row must contain exactly the supported fields")
    description = _clean_text(
        raw.get("description"), max_length=255, field="description", required=True
    )
    unit = _clean_text(raw.get("unit"), max_length=32, field="unit") or ""
    currency = _clean_text(raw.get("currency"), max_length=3, field="currency", required=True)
    if currency is None or len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a three-letter code")
    price = raw.get("unit_price_cents")
    if not isinstance(price, int) or isinstance(price, bool) or price < 0 or price > 2_147_483_647:
        raise ValueError("unit_price_cents is out of range")
    return {
        "description": description,
        "quantity": _decimal_text(
            raw.get("quantity"), field="quantity", maximum=Decimal("99999999.99")
        ),
        "unit": unit,
        "unit_price_cents": price,
        "vat_rate": _decimal_text(raw.get("vat_rate"), field="vat_rate", maximum=Decimal("100.00")),
        "currency": currency.upper(),
    }


def _contact_export_row(contact: object) -> dict[str, Any]:
    row: dict[str, Any] = {"name": str(getattr(contact, "name", "") or "").strip()}
    for field in CONTACT_FIELDS[1:]:
        value = getattr(contact, field, None)
        if value is not None:
            row[field] = bool(value) if field == "registry_auto_update" else str(value).strip()
    return _normalise_contact(row)


def _catalog_export_row(item: object) -> dict[str, Any]:
    return _normalise_catalog_item(
        {
            "description": str(getattr(item, "description", "") or "").strip(),
            "quantity": format(Decimal(str(getattr(item, "quantity", "0"))), "f"),
            "unit": str(getattr(item, "unit", "") or "").strip(),
            "unit_price_cents": int(getattr(item, "unit_price_cents", 0) or 0),
            "vat_rate": format(Decimal(str(getattr(item, "vat_rate", "0"))), "f"),
            "currency": str(getattr(item, "currency", "") or "").strip(),
        }
    )


def build_native_backup_bytes(
    *,
    contacts: list[object],
    catalog_items: list[object],
    max_rows: int = MAX_ROWS_PER_DATASET,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> bytes:
    """Build the exact three-member v1 archive in deterministic row order."""

    if len(contacts) > max_rows or len(catalog_items) > max_rows:
        raise ValueError(f"Native backup supports at most {max_rows} rows per dataset")
    contact_rows = sorted(
        (_contact_export_row(row) for row in contacts), key=lambda row: _canonical_json(row)
    )
    catalog_rows = sorted(
        (_catalog_export_row(row) for row in catalog_items), key=lambda row: _canonical_json(row)
    )
    datasets = (
        ("contacts", "contacts.jsonl", contact_rows),
        ("catalog_items", "catalog_items.jsonl", catalog_rows),
    )
    content: dict[str, bytes] = {filename: _jsonl(rows) for _name, filename, rows in datasets}
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "generated_at_utc": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "export_id": str(uuid4()),
        "datasets": [
            {
                "name": name,
                "filename": filename,
                "schema_version": SCHEMA_VERSION,
                "row_count": len(rows),
                "sha256": _digest(content[filename]),
            }
            for name, filename, rows in datasets
        ],
    }
    content["manifest.json"] = _canonical_json(manifest) + b"\n"
    if any(len(value) > max_member_bytes for value in content.values()):
        raise ValueError("Native backup dataset exceeds the configured import size limit")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for filename in MEMBERS:
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content[filename])
    payload = out.getvalue()
    if len(payload) > max_archive_bytes:
        raise ValueError("Native backup exceeds the configured import upload size limit")
    return payload


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(name) and not path.is_absolute() and ".." not in path.parts and len(path.parts) == 1


def _read_jsonl(data: bytes, *, normalizer, row_limit: int, label: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    if text and not text.endswith("\n"):
        raise ValueError(f"{label} must end with a newline")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"{label} has an empty row")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has malformed JSONL at row {line_no}") from exc
        rows.append(normalizer(value))
        if len(rows) > row_limit:
            raise ValueError(f"{label} has too many rows")
    return rows


def parse_native_backup_bytes(
    data: bytes,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_rows: int = MAX_ROWS_PER_DATASET,
) -> NativeBackup:
    """Validate every byte before returning any importable rows.

    ZIP paths, member names, duplicate entries, schema, checksums and declared
    counts are all validated before callers can mutate the database.
    """

    if not data or len(data) > max_total_bytes:
        raise ValueError("backup archive is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("backup is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (
            len(infos) != len(MEMBERS)
            or set(names) != set(MEMBERS)
            or len(set(names)) != len(names)
        ):
            raise ValueError(
                "backup must contain exactly manifest.json, contacts.jsonl and catalog_items.jsonl"
            )
        if any(info.is_dir() or not _safe_member_name(info.filename) for info in infos):
            raise ValueError("backup contains an unsafe ZIP member")
        total_size = sum(max(0, int(info.file_size)) for info in infos)
        if total_size > max_total_bytes or any(
            int(info.file_size) > max_member_bytes for info in infos
        ):
            raise ValueError("backup expands beyond the allowed size")
        content: dict[str, bytes] = {}
        for info in infos:
            with archive.open(info) as member:
                value = member.read(max_member_bytes + 1)
            if len(value) != int(info.file_size) or len(value) > max_member_bytes:
                raise ValueError("backup member exceeds the allowed size")
            content[info.filename] = value
    try:
        manifest = json.loads(content["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "format",
        "version",
        "generated_at_utc",
        "export_id",
        "datasets",
    }:
        raise ValueError("backup manifest has an unsupported schema")
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ValueError("backup format or version is unsupported")
    if not isinstance(manifest.get("export_id"), str):
        raise ValueError("backup manifest export_id is invalid")
    try:
        if str(UUID(manifest["export_id"])) != manifest["export_id"]:
            raise ValueError("backup manifest export_id is invalid")
    except (ValueError, AttributeError) as exc:
        raise ValueError("backup manifest export_id is invalid") from exc
    if not isinstance(manifest.get("generated_at_utc"), str):
        raise ValueError("backup manifest generated_at_utc is invalid")
    try:
        generated_at = datetime.fromisoformat(manifest["generated_at_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("backup manifest generated_at_utc is invalid") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() != UTC.utcoffset(generated_at):
        raise ValueError("backup manifest generated_at_utc must be UTC")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 2:
        raise ValueError("backup manifest datasets are invalid")
    by_name = {entry.get("name"): entry for entry in datasets if isinstance(entry, dict)}
    expected = {"contacts": "contacts.jsonl", "catalog_items": "catalog_items.jsonl"}
    if set(by_name) != set(expected):
        raise ValueError("backup manifest datasets are invalid")
    parsed: dict[str, list[dict[str, Any]]] = {}
    normalizers = {"contacts": _normalise_contact, "catalog_items": _normalise_catalog_item}
    for name, filename in expected.items():
        entry = by_name[name]
        if (
            set(entry) != {"name", "filename", "schema_version", "row_count", "sha256"}
            or entry.get("filename") != filename
            or entry.get("schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("backup manifest dataset schema is invalid")
        if (
            not isinstance(entry.get("row_count"), int)
            or isinstance(entry.get("row_count"), bool)
            or entry["row_count"] < 0
            or entry["row_count"] > max_rows
        ):
            raise ValueError("backup manifest row count is invalid")
        if (
            not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
            or entry["sha256"].lower() != _digest(content[filename])
        ):
            raise ValueError("backup dataset checksum does not match")
        rows = _read_jsonl(
            content[filename], normalizer=normalizers[name], row_limit=max_rows, label=filename
        )
        if len(rows) != entry["row_count"]:
            raise ValueError("backup dataset row count does not match")
        parsed[name] = rows
    return NativeBackup(
        manifest=manifest, contacts=parsed["contacts"], catalog_items=parsed["catalog_items"]
    )


def _load_run_payload(run: object, *, import_storage_root: Path) -> bytes:
    relative = str(getattr(run, "file_path", "") or "").strip()
    if not relative:
        raise ValueError("Import run has no stored file")
    root = import_storage_root.resolve()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise ValueError("Import file is unavailable")
    return target.read_bytes()


def _validated_run_backup(
    run: object, *, import_storage_root: Path, max_upload_bytes: int
) -> NativeBackup:
    payload = _load_run_payload(run, import_storage_root=import_storage_root)
    if len(payload) > max_upload_bytes:
        raise ValueError("backup archive is too large")
    expected_file_hash = str(getattr(run, "file_sha256", "") or "").strip().lower()
    if expected_file_hash and expected_file_hash != _digest(payload):
        raise ValueError("stored backup checksum does not match")
    return parse_native_backup_bytes(
        payload,
        max_member_bytes=max_upload_bytes,
        max_total_bytes=max_upload_bytes * len(MEMBERS),
    )


def _legacy_preview_native_backup_import(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> dict[str, Any]:
    """Return a complete validated preview without writing data."""

    from sqlalchemy import func, select

    from fakturek.importing import lookup_imported_id
    from fakturek.models import Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")
    backup = _validated_run_backup(
        run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
    )
    config = _run_config(run)
    contact_mode = _contact_mode(config)
    contacts = {"parsed": len(backup.contacts), "will_create": 0, "will_reuse": 0, "will_skip": 0}
    catalog = {"parsed": len(backup.catalog_items), "will_create": 0, "will_reuse": 0}
    for row in backup.contacts:
        existing = _find_contact(
            db,
            subject_id=subject_id,
            row=row,
            conflict_mode=contact_mode,
            lookup=lookup_imported_id,
            select=select,
        )
        if existing is None:
            contacts["will_create"] += 1
        else:
            contacts["will_reuse"] += 1
            if contact_mode == "skip_existing":
                contacts["will_skip"] += 1
    for row in backup.catalog_items:
        existing = _find_catalog_item(
            db, subject_id=subject_id, row=row, lookup=lookup_imported_id, select=select, func=func
        )
        if existing is None:
            catalog["will_create"] += 1
        else:
            catalog["will_reuse"] += 1
    return {
        "native_backup": True,
        "source": SOURCE,
        "ready_note": (
            "The ZIP was fully validated. Processing imports contacts and catalog items only."
        ),
        "manifest": backup.manifest,
        "datasets": {
            "contacts": {"row_count": len(backup.contacts), "checksum": "verified"},
            "catalog_items": {"row_count": len(backup.catalog_items), "checksum": "verified"},
        },
        "contacts": contacts,
        "catalog_items": catalog,
        "invoices": {
            "will_import": 0,
            "already_imported": 0,
            "number_conflicts": 0,
            "will_renumber": 0,
            "will_create_contacts": 0,
            "will_reuse_contacts": 0,
            "warnings": [],
            "errors": [],
        },
    }


def _run_config(run: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(getattr(run, "summary_json", "") or ""))
    except json.JSONDecodeError:
        return {}
    return dict(payload.get("config") or {}) if isinstance(payload, dict) else {}


def _contact_mode(config: dict[str, Any]) -> str:
    value = str(config.get("contact_conflict_mode") or "merge_existing").strip().lower()
    return value if value in {"merge_existing", "skip_existing", "create_new"} else "merge_existing"


def _find_contact(db, *, subject_id: int, row: dict[str, Any], conflict_mode: str, lookup, select):
    from fakturek.models import Contact

    mapped = lookup(
        db,
        subject_id=int(subject_id),
        source=SOURCE,
        entity_type="contact",
        external_id=row_identity(row),
    )
    if mapped is not None:
        found = db.scalar(
            select(Contact)
            .where(Contact.subject_id == int(subject_id))
            .where(Contact.id == int(mapped))
            .limit(1)
        )
        if found is not None:
            return found
    if conflict_mode == "create_new":
        return None
    source = row.get("external_source")
    external_id = row.get("external_id")
    if source and external_id:
        found = db.scalar(
            select(Contact)
            .where(Contact.subject_id == int(subject_id))
            .where(Contact.external_source == source)
            .where(Contact.external_id == external_id)
            .limit(1)
        )
        if found is not None:
            return found
    for field in ("ico", "email", "name"):
        if row.get(field):
            found = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(getattr(Contact, field) == row[field])
                .limit(1)
            )
            if found is not None:
                return found
    return None


def _find_catalog_item(db, *, subject_id: int, row: dict[str, Any], lookup, select, func):
    from fakturek.models import InvoiceCatalogItem

    mapped = lookup(
        db,
        subject_id=int(subject_id),
        source=SOURCE,
        entity_type="catalog_item",
        external_id=row_identity(row),
    )
    if mapped is not None:
        found = db.scalar(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .where(InvoiceCatalogItem.id == int(mapped))
            .limit(1)
        )
        if found is not None:
            return found
    return db.scalar(
        select(InvoiceCatalogItem)
        .where(InvoiceCatalogItem.subject_id == int(subject_id))
        .where(InvoiceCatalogItem.currency == row["currency"])
        .where(
            func.lower(func.trim(InvoiceCatalogItem.description)) == row["description"].casefold()
        )
        .where(InvoiceCatalogItem.quantity == Decimal(row["quantity"]))
        .where(InvoiceCatalogItem.unit == row["unit"])
        .where(InvoiceCatalogItem.unit_price_cents == int(row["unit_price_cents"]))
        .where(InvoiceCatalogItem.vat_rate == Decimal(row["vat_rate"]))
        .limit(1)
    )


def _legacy_process_native_backup_import(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> dict[str, Any]:
    """Validate first, then import the two v1 datasets transactionally."""

    from sqlalchemy import func, select

    from fakturek.importing import ensure_import_map, lookup_imported_id
    from fakturek.models import Contact, InvoiceCatalogItem, Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")
    # This is intentionally before the first database mutation.
    backup = _validated_run_backup(
        run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
    )
    config = _run_config(run)
    contact_mode = _contact_mode(config)
    summary: dict[str, Any] = {
        "phase": "native_backup_v1",
        "source": SOURCE,
        "backup": {
            "version": VERSION,
            "datasets": backup.manifest["datasets"],
            "checksum_state": "verified",
        },
        "config": {"contact_conflict_mode": contact_mode},
        "contacts": {
            "parsed": len(backup.contacts),
            "created": 0,
            "reused": 0,
            "skipped_existing": 0,
        },
        "catalog_items": {"parsed": len(backup.catalog_items), "created": 0, "reused": 0},
    }
    for row in backup.contacts:
        identity = row_identity(row)
        contact = _find_contact(
            db,
            subject_id=subject_id,
            row=row,
            conflict_mode=contact_mode,
            lookup=lookup_imported_id,
            select=select,
        )
        if contact is None:
            contact = Contact(subject_id=int(subject_id), name=row["name"])
            for field in CONTACT_FIELDS[1:]:
                if field in row:
                    setattr(contact, field, row[field])
            db.add(contact)
            db.flush()
            summary["contacts"]["created"] += 1
        else:
            summary["contacts"]["reused"] += 1
            if contact_mode == "skip_existing":
                summary["contacts"]["skipped_existing"] += 1
            elif contact_mode == "merge_existing":
                for field in CONTACT_FIELDS[1:]:
                    if field == "registry_auto_update":
                        continue
                    incoming = row.get(field)
                    if incoming is not None and not getattr(contact, field, None):
                        setattr(contact, field, incoming)
                db.add(contact)
        ensure_import_map(
            db,
            subject_id=int(subject_id),
            source=SOURCE,
            entity_type="contact",
            external_id=identity,
            internal_id=int(contact.id),
        )
    for row in backup.catalog_items:
        identity = row_identity(row)
        item = _find_catalog_item(
            db, subject_id=subject_id, row=row, lookup=lookup_imported_id, select=select, func=func
        )
        if item is None:
            item = InvoiceCatalogItem(
                subject_id=int(subject_id),
                description=row["description"],
                quantity=Decimal(row["quantity"]),
                unit=row["unit"],
                unit_price_cents=int(row["unit_price_cents"]),
                vat_rate=Decimal(row["vat_rate"]),
                currency=row["currency"],
            )
            db.add(item)
            db.flush()
            summary["catalog_items"]["created"] += 1
        else:
            summary["catalog_items"]["reused"] += 1
        ensure_import_map(
            db,
            subject_id=int(subject_id),
            source=SOURCE,
            entity_type="catalog_item",
            external_id=identity,
            internal_id=int(item.id),
        )
    summary["note"] = (
        f"contacts: +{summary['contacts']['created']}; "
        f"catalog items: +{summary['catalog_items']['created']}"
    )
    return summary


# ---------------------------------------------------------------------------
# v1 planning and application.  These supersede the original helpers above.
# ---------------------------------------------------------------------------


def _snapshot_matches_contact(row: dict[str, Any], contacts: list[object]):
    for existing in contacts:
        if (
            row.get("external_source")
            and row.get("external_id")
            and (
                getattr(existing, "external_source", None) == row["external_source"]
                and getattr(existing, "external_id", None) == row["external_id"]
            )
        ):
            return existing
    for field in ("ico", "email", "name"):
        if row.get(field):
            for existing in contacts:
                if getattr(existing, field, None) == row[field]:
                    return existing
    return None


def _snapshot_matches_catalog(row: dict[str, Any], items: list[object]):
    signature = (
        row["description"].casefold(),
        Decimal(row["quantity"]),
        row["unit"],
        int(row["unit_price_cents"]),
        Decimal(row["vat_rate"]),
        row["currency"],
    )
    for existing in items:
        candidate = (
            str(getattr(existing, "description", "") or "").strip().casefold(),
            Decimal(str(getattr(existing, "quantity", "0"))),
            str(getattr(existing, "unit", "") or ""),
            int(getattr(existing, "unit_price_cents", 0) or 0),
            Decimal(str(getattr(existing, "vat_rate", "0"))),
            str(getattr(existing, "currency", "") or ""),
        )
        if candidate == signature:
            return existing
    return None


def build_native_import_plan(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> NativeImportPlan:
    """Validate and plan against one pre-existing tenant snapshot only."""

    from sqlalchemy import select

    from fakturek.importing import lookup_imported_id
    from fakturek.models import Contact, InvoiceCatalogItem, Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")
    backup = _validated_run_backup(
        run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
    )
    contact_mode = _contact_mode(_run_config(run))
    snapshot_contacts = list(
        db.scalars(select(Contact).where(Contact.subject_id == int(subject_id))).all()
    )
    snapshot_catalog = list(
        db.scalars(
            select(InvoiceCatalogItem).where(InvoiceCatalogItem.subject_id == int(subject_id))
        ).all()
    )
    contact_by_id = {int(item.id): item for item in snapshot_contacts}
    catalog_by_id = {int(item.id): item for item in snapshot_catalog}

    def _actions(
        rows, *, entity_type: str, snapshot: list[object], by_id: dict[int, object], matcher
    ):
        actions: list[NativeImportAction] = []
        seen_identities: set[str] = set()
        for row in rows:
            identity = row_identity(row)
            if identity in seen_identities:
                actions.append(NativeImportAction(identity=identity, row=row, action="reuse"))
                continue
            seen_identities.add(identity)
            mapped_id = lookup_imported_id(
                db,
                subject_id=int(subject_id),
                source=SOURCE,
                entity_type=entity_type,
                external_id=identity,
            )
            existing = by_id.get(int(mapped_id)) if mapped_id is not None else None
            if existing is None and (entity_type != "contact" or contact_mode != "create_new"):
                existing = matcher(row, snapshot)
            actions.append(
                NativeImportAction(
                    identity=identity,
                    row=row,
                    action="reuse" if existing is not None else "create",
                    existing_id=int(existing.id) if existing is not None else None,
                )
            )
        return actions

    return NativeImportPlan(
        backup=backup,
        contact_mode=contact_mode,
        contacts=_actions(
            backup.contacts,
            entity_type="contact",
            snapshot=snapshot_contacts,
            by_id=contact_by_id,
            matcher=_snapshot_matches_contact,
        ),
        catalog_items=_actions(
            backup.catalog_items,
            entity_type="catalog_item",
            snapshot=snapshot_catalog,
            by_id=catalog_by_id,
            matcher=_snapshot_matches_catalog,
        ),
    )


def _preview_from_plan(plan: NativeImportPlan) -> dict[str, Any]:
    contacts = {"parsed": len(plan.contacts), "will_create": 0, "will_reuse": 0, "will_skip": 0}
    catalog = {"parsed": len(plan.catalog_items), "will_create": 0, "will_reuse": 0}
    for action in plan.contacts:
        contacts[f"will_{action.action}"] += 1
        if (
            action.action == "reuse"
            and action.existing_id is not None
            and plan.contact_mode == "skip_existing"
        ):
            contacts["will_skip"] += 1
    for action in plan.catalog_items:
        catalog[f"will_{action.action}"] += 1
    return {
        "native_backup": True,
        "source": SOURCE,
        "ready_note": (
            "The ZIP was fully validated. Processing imports contacts and catalog items only."
        ),
        "manifest": plan.backup.manifest,
        "datasets": {
            "contacts": {"row_count": len(plan.contacts), "checksum": "verified"},
            "catalog_items": {"row_count": len(plan.catalog_items), "checksum": "verified"},
        },
        "contacts": contacts,
        "catalog_items": catalog,
        "invoices": {
            "will_import": 0,
            "already_imported": 0,
            "number_conflicts": 0,
            "will_renumber": 0,
            "will_create_contacts": 0,
            "will_reuse_contacts": 0,
            "warnings": [],
            "errors": [],
        },
    }


def preview_native_backup_import(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> dict[str, Any]:
    return _preview_from_plan(
        build_native_import_plan(
            db,
            run=run,
            subject_id=subject_id,
            import_storage_root=import_storage_root,
            max_upload_bytes=max_upload_bytes,
        )
    )


def _bind_map_to_canonical_entity(
    db, *, subject_id: int, entity_type: str, identity: str, entity, model
):
    """Return the ImportMap winner and delete any speculative losing row."""

    from sqlalchemy import select

    from fakturek.importing import ensure_import_map, lookup_imported_id

    ensure_import_map(
        db,
        subject_id=int(subject_id),
        source=SOURCE,
        entity_type=entity_type,
        external_id=identity,
        internal_id=int(entity.id),
    )
    mapped_id = lookup_imported_id(
        db,
        subject_id=int(subject_id),
        source=SOURCE,
        entity_type=entity_type,
        external_id=identity,
    )
    if mapped_id == int(entity.id):
        return entity, False
    db.delete(entity)
    db.flush()
    winner = db.scalar(
        select(model)
        .where(model.subject_id == int(subject_id))
        .where(model.id == int(mapped_id))
        .limit(1)
    )
    if winner is None:
        raise ValueError("Native backup mapping points to an unavailable record")
    return winner, True


def process_native_backup_import(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> dict[str, Any]:
    """Build one plan, then apply exactly that plan without fuzzy re-matching."""

    from sqlalchemy import select

    from fakturek.models import Contact, InvoiceCatalogItem

    plan = build_native_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )
    summary: dict[str, Any] = {
        "phase": "native_backup_v1",
        "source": SOURCE,
        "backup": {
            "version": VERSION,
            "datasets": plan.backup.manifest["datasets"],
            "checksum_state": "verified",
        },
        "config": {"contact_conflict_mode": plan.contact_mode},
        "contacts": {
            "parsed": len(plan.contacts),
            "created": 0,
            "reused": 0,
            "skipped_existing": 0,
        },
        "catalog_items": {"parsed": len(plan.catalog_items), "created": 0, "reused": 0},
    }
    resolved_contacts: dict[str, object] = {}
    for action in plan.contacts:
        contact = resolved_contacts.get(action.identity)
        if contact is not None:
            summary["contacts"]["reused"] += 1
            continue
        if action.action == "reuse":
            contact = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(Contact.id == action.existing_id)
                .limit(1)
            )
            if contact is None:
                raise ValueError("Native backup contact changed during processing")
            summary["contacts"]["reused"] += 1
            if plan.contact_mode == "skip_existing":
                summary["contacts"]["skipped_existing"] += 1
            elif plan.contact_mode == "merge_existing":
                for field in CONTACT_FIELDS[1:]:
                    if field == "registry_auto_update":
                        continue
                    if action.row.get(field) is not None and not getattr(contact, field, None):
                        setattr(contact, field, action.row[field])
                db.add(contact)
        else:
            contact = Contact(subject_id=int(subject_id), name=action.row["name"])
            for field in CONTACT_FIELDS[1:]:
                if field in action.row:
                    setattr(contact, field, action.row[field])
            db.add(contact)
            db.flush()
            contact, lost_race = _bind_map_to_canonical_entity(
                db,
                subject_id=subject_id,
                entity_type="contact",
                identity=action.identity,
                entity=contact,
                model=Contact,
            )
            summary["contacts"]["reused" if lost_race else "created"] += 1
        resolved_contacts[action.identity] = contact
        if action.action == "reuse":
            _bind_map_to_canonical_entity(
                db,
                subject_id=subject_id,
                entity_type="contact",
                identity=action.identity,
                entity=contact,
                model=Contact,
            )

    resolved_catalog: dict[str, object] = {}
    for action in plan.catalog_items:
        item = resolved_catalog.get(action.identity)
        if item is not None:
            summary["catalog_items"]["reused"] += 1
            continue
        if action.action == "reuse":
            item = db.scalar(
                select(InvoiceCatalogItem)
                .where(InvoiceCatalogItem.subject_id == int(subject_id))
                .where(InvoiceCatalogItem.id == action.existing_id)
                .limit(1)
            )
            if item is None:
                raise ValueError("Native backup catalog item changed during processing")
            summary["catalog_items"]["reused"] += 1
        else:
            item = InvoiceCatalogItem(
                subject_id=int(subject_id),
                description=action.row["description"],
                quantity=Decimal(action.row["quantity"]),
                unit=action.row["unit"],
                unit_price_cents=int(action.row["unit_price_cents"]),
                vat_rate=Decimal(action.row["vat_rate"]),
                currency=action.row["currency"],
            )
            db.add(item)
            db.flush()
            item, lost_race = _bind_map_to_canonical_entity(
                db,
                subject_id=subject_id,
                entity_type="catalog_item",
                identity=action.identity,
                entity=item,
                model=InvoiceCatalogItem,
            )
            summary["catalog_items"]["reused" if lost_race else "created"] += 1
        resolved_catalog[action.identity] = item
        if action.action == "reuse":
            _bind_map_to_canonical_entity(
                db,
                subject_id=subject_id,
                entity_type="catalog_item",
                identity=action.identity,
                entity=item,
                model=InvoiceCatalogItem,
            )

    summary["note"] = (
        f"contacts: +{summary['contacts']['created']}; "
        f"catalog items: +{summary['catalog_items']['created']}"
    )
    return summary

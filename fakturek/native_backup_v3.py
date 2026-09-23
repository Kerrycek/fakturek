"""Strict Fakturek-native backup v3 with portable invoice series.

This deliberately extends v2 without changing its wire format or importer.
Only the five explicit master-data projections below ever cross this boundary.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from fakturek.bank_account_lock import lock_subject_bank_account_mutations
from fakturek.invoice_numbering import (
    SIGNED_INTEGER_MAX,
    InvoiceSeriesCounterExhausted,
    next_invoice_series_counter,
)
from fakturek.native_backup import (
    CONTACT_FIELDS,
    _catalog_signature,
    _contact_match_key,
    _contact_mode,
    _normalise_catalog_item,
    _normalise_contact,
    _run_config,
    _snapshot_indexes,
)
from fakturek.native_backup_v2 import (
    _account_export_row,
    _account_identity,
    _account_key,
    _canonical,
    _digest,
    _freeze,
    _jsonl,
    _normalise_bank_account,
    _read_jsonl,
    _safe_member_name,
    _stored_account_key,
    _thaw,
)

FORMAT = "fakturek-native-backup"
VERSION = 3
SOURCE = "fakturek_native_v3"
MEDIA_TYPE = "application/vnd.fakturek.native-backup-v3+zip"
MEMBERS = (
    "manifest.json",
    "contacts.jsonl",
    "catalog_items.jsonl",
    "bank_accounts.jsonl",
    "invoice_series.jsonl",
)
SCHEMA_VERSION = 1
MAX_ROWS_PER_DATASET = 100_000
MAX_BANK_ACCOUNTS = 1_000
MAX_INVOICE_SERIES = 1_000
SERIES_FIELDS = ("name", "prefix", "pad_length", "last_counter", "last_counter_year")
_YEAR_MIN, _YEAR_MAX = 1900, 9999
_INTEGER_MAX = SIGNED_INTEGER_MAX


@dataclass(frozen=True)
class BackupV3:
    manifest: Mapping[str, Any]
    contacts: tuple[Mapping[str, Any], ...]
    catalog_items: tuple[Mapping[str, Any], ...]
    bank_accounts: tuple[Mapping[str, Any], ...]
    invoice_series: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Action:
    identity: str
    row: Mapping[str, Any]
    action: str
    existing_id: int | None = None
    import_default: bool = False


@dataclass(frozen=True)
class ImportPlanV3:
    backup: BackupV3
    contact_mode: str
    contacts: tuple[Action, ...]
    catalog_items: tuple[Action, ...]
    bank_accounts: tuple[Action, ...]
    invoice_series: tuple[Action, ...]
    warnings: tuple[str, ...]
    contact_snapshot: tuple[tuple[int, tuple[tuple[str, object], ...]], ...]
    catalog_snapshot: tuple[tuple[int, tuple[object, ...]], ...]
    account_snapshot: tuple[tuple[int, tuple[str, ...], bool, int], ...]
    series_snapshot: tuple[tuple[int, str, str, int, int, int | None], ...]
    series_preview: tuple[Mapping[str, Any], ...]
    series_warnings: tuple[str, ...]


def _text(value: object, *, field: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invoice series {field} must be a string")
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError(f"invoice series {field} is invalid")
    return value


def _normalise_series(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(SERIES_FIELDS):
        raise ValueError("invoice series row must contain exactly the supported fields")
    name = _text(raw["name"], field="name", maximum=100, required=True)
    prefix = _text(raw["prefix"], field="prefix", maximum=50)
    for field, low, high in (("pad_length", 1, 20), ("last_counter", 0, _INTEGER_MAX)):
        value = raw[field]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f"invoice series {field} is invalid")
    year = raw["last_counter_year"]
    if year is not None and (
        not isinstance(year, int) or isinstance(year, bool) or not _YEAR_MIN <= year <= _YEAR_MAX
    ):
        raise ValueError("invoice series last_counter_year is invalid")
    return {
        "name": name,
        "prefix": prefix,
        "pad_length": raw["pad_length"],
        "last_counter": raw["last_counter"],
        "last_counter_year": year,
    }


def _series_export_row(series: object) -> dict[str, Any]:
    # Literal projection: no invoice, subject, profile, or settings fields leak.
    return _normalise_series(
        {
            "name": str(getattr(series, "name", "") or ""),
            "prefix": str(getattr(series, "prefix", "") or ""),
            "pad_length": int(getattr(series, "pad_length", 0) or 0),
            "last_counter": int(getattr(series, "last_counter", 0) or 0),
            "last_counter_year": getattr(series, "last_counter_year", None),
        }
    )


def _contact_export_row(contact: object) -> dict[str, Any]:
    row: dict[str, Any] = {"name": str(getattr(contact, "name", "") or "").strip()}
    for field in CONTACT_FIELDS[1:]:
        value = getattr(contact, field, None)
        if value is not None:
            row[field] = bool(value) if field == "registry_auto_update" else str(value).strip()
    return _normalise_contact(row)


def _catalog_export_row(item: object) -> dict[str, Any]:
    from decimal import Decimal

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


def build_native_backup_v3_bytes(
    *,
    contacts: list[object],
    catalog_items: list[object],
    bank_accounts: list[object],
    invoice_series: list[object],
    max_rows: int = MAX_ROWS_PER_DATASET,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> bytes:
    if (
        len(contacts) > max_rows
        or len(catalog_items) > max_rows
        or len(bank_accounts) > MAX_BANK_ACCOUNTS
        or len(invoice_series) > MAX_INVOICE_SERIES
    ):
        raise ValueError("Native backup v3 dataset exceeds its row limit")
    accounts = sorted(
        (_account_export_row(x) for x in bank_accounts),
        key=lambda r: (r["sort_order"], _canonical(r)),
    )
    if (
        len({_account_key(r) for r in accounts}) != len(accounts)
        or sum(bool(r["is_default"]) for r in accounts) > 1
    ):
        raise ValueError("Native backup v3 bank accounts are invalid")
    series = sorted(
        (_series_export_row(x) for x in invoice_series),
        key=lambda r: (r["name"].casefold(), _canonical(r)),
    )
    if len({_series_storage_key(r["name"]) for r in series}) != len(series):
        raise ValueError("Native backup v3 contains colliding invoice series names")
    datasets = (
        (
            "contacts",
            "contacts.jsonl",
            sorted((_contact_export_row(x) for x in contacts), key=_canonical),
        ),
        (
            "catalog_items",
            "catalog_items.jsonl",
            sorted((_catalog_export_row(x) for x in catalog_items), key=_canonical),
        ),
        ("bank_accounts", "bank_accounts.jsonl", accounts),
        ("invoice_series", "invoice_series.jsonl", series),
    )
    content = {filename: _jsonl(rows) for _name, filename, rows in datasets}
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
    content["manifest.json"] = _canonical(manifest) + b"\n"
    if any(len(value) > max_member_bytes for value in content.values()):
        raise ValueError("Native backup v3 dataset exceeds the configured import size limit")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for filename in MEMBERS:
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content[filename])
    payload = out.getvalue()
    if len(payload) > max_archive_bytes:
        raise ValueError("Native backup v3 exceeds the configured import upload size limit")
    return payload


def parse_native_backup_v3_bytes(
    data: bytes,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_rows: int = MAX_ROWS_PER_DATASET,
) -> BackupV3:
    if not data or len(data) > max_total_bytes:
        raise ValueError("backup archive is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("backup is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [x.filename for x in infos]
        if (
            len(infos) != len(MEMBERS)
            or set(names) != set(MEMBERS)
            or len(set(names)) != len(names)
        ):
            raise ValueError("backup v3 must contain exactly its declared members")
        if any(x.is_dir() or not _safe_member_name(x.filename) for x in infos):
            raise ValueError("backup contains an unsafe ZIP member")
        if sum(max(0, int(x.file_size)) for x in infos) > max_total_bytes or any(
            int(x.file_size) > max_member_bytes for x in infos
        ):
            raise ValueError("backup expands beyond the allowed size")
        content = {}
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
        raise ValueError("backup format, version or manifest schema is unsupported")
    if (
        not isinstance(manifest["format"], str)
        or manifest["format"] != FORMAT
        or not isinstance(manifest["version"], int)
        or isinstance(manifest["version"], bool)
        or manifest["version"] != VERSION
        or not isinstance(manifest["export_id"], str)
        or not isinstance(manifest["generated_at_utc"], str)
    ):
        raise ValueError("backup format, version or manifest schema is unsupported")
    try:
        if str(UUID(manifest["export_id"])) != manifest["export_id"]:
            raise ValueError
        stamp = datetime.fromisoformat(manifest["generated_at_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("backup manifest identity or timestamp is invalid") from exc
    expected = {
        "contacts": ("contacts.jsonl", _normalise_contact, max_rows),
        "catalog_items": ("catalog_items.jsonl", _normalise_catalog_item, max_rows),
        "bank_accounts": ("bank_accounts.jsonl", _normalise_bank_account, MAX_BANK_ACCOUNTS),
        "invoice_series": ("invoice_series.jsonl", _normalise_series, MAX_INVOICE_SERIES),
    }
    entries = manifest.get("datasets")
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("backup manifest datasets are invalid")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "filename",
            "schema_version",
            "row_count",
            "sha256",
        }:
            raise ValueError("backup manifest dataset schema is invalid")
        if (
            not isinstance(entry["name"], str)
            or not isinstance(entry["filename"], str)
            or not isinstance(entry["schema_version"], int)
            or isinstance(entry["schema_version"], bool)
            or not isinstance(entry["row_count"], int)
            or isinstance(entry["row_count"], bool)
            or not isinstance(entry["sha256"], str)
        ):
            raise ValueError("backup manifest dataset schema is invalid")
    by_name = {entry["name"]: entry for entry in entries}
    if set(by_name) != set(expected):
        raise ValueError("backup manifest datasets are invalid")
    parsed = {}
    for name, (filename, normalizer, limit) in expected.items():
        entry = by_name[name]
        if entry["filename"] != filename or entry["schema_version"] != SCHEMA_VERSION:
            raise ValueError("backup manifest dataset schema is invalid")
        if not 0 <= entry["row_count"] <= limit:
            raise ValueError("backup manifest row count is invalid")
        if len(entry["sha256"]) != 64 or entry["sha256"].lower() != _digest(content[filename]):
            raise ValueError("backup dataset checksum does not match")
        parsed[name] = _read_jsonl(
            content[filename], normalizer=normalizer, row_limit=limit, label=filename
        )
        if len(parsed[name]) != entry["row_count"]:
            raise ValueError("backup dataset row count does not match")
    if (
        len({_account_key(r) for r in parsed["bank_accounts"]}) != len(parsed["bank_accounts"])
        or sum(bool(r["is_default"]) for r in parsed["bank_accounts"]) > 1
    ):
        raise ValueError("backup contains invalid bank accounts")
    if len({_series_storage_key(r["name"]) for r in parsed["invoice_series"]}) != len(
        parsed["invoice_series"]
    ):
        raise ValueError("backup contains colliding normalized invoice series names")
    return BackupV3(
        _freeze(manifest),
        tuple(_freeze(r) for r in parsed["contacts"]),
        tuple(_freeze(r) for r in parsed["catalog_items"]),
        tuple(_freeze(r) for r in parsed["bank_accounts"]),
        tuple(_freeze(r) for r in parsed["invoice_series"]),
    )


def _load_backup(run: object, *, import_storage_root: Path, max_upload_bytes: int) -> BackupV3:
    relative = str(getattr(run, "file_path", "") or "").strip()
    root = import_storage_root.resolve()
    target = (root / relative).resolve()
    if not relative or root not in target.parents or not target.is_file():
        raise ValueError("Import file is unavailable")
    payload = target.read_bytes()
    if len(payload) > max_upload_bytes:
        raise ValueError("backup archive is too large")
    stored_hash = str(getattr(run, "file_sha256", "") or "").strip().lower()
    if stored_hash and stored_hash != _digest(payload):
        raise ValueError("stored backup checksum does not match")
    return parse_native_backup_v3_bytes(
        payload, max_member_bytes=max_upload_bytes, max_total_bytes=max_upload_bytes * len(MEMBERS)
    )


def _series_key(row: Mapping[str, Any]) -> str:
    return str(row["name"]).casefold()


def _series_storage_key(name: object) -> str:
    """Conservatively approximate case/accent-insensitive SQL name equality."""
    decomposed = unicodedata.normalize("NFKD", str(name or "").strip().casefold())
    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character) and unicodedata.category(character) != "Cf"
    )


def _series_identity(row: Mapping[str, Any]) -> str:
    return _digest(_canonical(_series_key(row)))


def _stored_series_snapshot(item: object) -> tuple[int, str, str, int, int, int | None]:
    year = getattr(item, "last_counter_year", None)
    return (
        int(item.id),
        str(getattr(item, "name", "") or "").casefold(),
        str(getattr(item, "prefix", "") or ""),
        int(getattr(item, "pad_length", 0) or 0),
        int(getattr(item, "last_counter", 0) or 0),
        int(year) if year is not None else None,
    )


def _stored_contact_keys(item: object) -> tuple[tuple[str, object], ...]:
    """Return every identity under which the snapshot index can match a contact."""
    keys: list[tuple[str, object]] = []
    source = str(getattr(item, "external_source", "") or "").strip()
    external_id = str(getattr(item, "external_id", "") or "").strip()
    if source and external_id:
        keys.append(("external", (source, external_id)))
    ico = str(getattr(item, "ico", "") or "").strip()
    if ico:
        keys.append(("ico", ico))
    email = str(getattr(item, "email", "") or "").strip()
    if email:
        keys.append(("email", email.casefold()))
    name = str(getattr(item, "name", "") or "").strip()
    if name:
        keys.append(("name", name))
    return tuple(keys)


def _stored_catalog_signature(item: object) -> tuple[object, ...]:
    from decimal import Decimal

    return (
        str(getattr(item, "description", "") or "").strip().casefold(),
        Decimal(str(getattr(item, "quantity", "0"))),
        str(getattr(item, "unit", "") or ""),
        int(getattr(item, "unit_price_cents", 0) or 0),
        Decimal(str(getattr(item, "vat_rate", "0"))),
        str(getattr(item, "currency", "") or ""),
    )


def _normalised_series_prefix(prefix: str | None, *, year: int) -> str:
    """Keep preview formatting aligned with the invoice-issuing formatter."""
    raw = str(prefix or "").strip()
    raw = re.sub(r"^20\d{2}[-_/\s]*", "", raw)
    raw = re.sub(r"[^A-Za-z0-9/_-]+", "-", raw).strip("-_/")
    if raw:
        return f"{int(year)}-{raw}-"
    return f"{int(year)}-"


def _format_series_number(row: Mapping[str, Any], counter: int, *, year: int) -> str:
    prefix = _normalised_series_prefix(str(row["prefix"]), year=int(year))
    pad = max(1, min(int(row["pad_length"]), 20))
    number = f"{prefix}{str(int(counter)).zfill(pad)}"
    if len(number) > 50:
        raise ValueError("invoice series next number would exceed the database limit")
    return number


def _series_row_from_model(series: object) -> dict[str, Any]:
    return _normalise_series(
        {
            "name": str(getattr(series, "name", "") or ""),
            "prefix": str(getattr(series, "prefix", "") or ""),
            "pad_length": int(getattr(series, "pad_length", 0) or 0),
            "last_counter": int(getattr(series, "last_counter", 0) or 0),
            "last_counter_year": getattr(series, "last_counter_year", None),
        }
    )


def _observed_series_counters_for_year(
    db,
    *,
    subject_id: int,
    prefixes: set[str],
    year: int,
) -> dict[str, int]:
    from sqlalchemy import select

    from fakturek.models import Invoice

    counters = {prefix: 0 for prefix in prefixes}
    if not prefixes:
        return counters
    numbers = db.scalars(
        select(Invoice.number)
        .where(Invoice.subject_id == int(subject_id))
        .where(Invoice.number.is_not(None))
        .where(Invoice.number.startswith(f"{int(year)}-"))
    ).all()
    for raw in numbers:
        value = str(raw or "").strip()
        separator = value.rfind("-")
        if separator < 0:
            continue
        prefix = value[: separator + 1]
        suffix = value[separator + 1 :]
        if prefix in counters and suffix.isdigit():
            counters[prefix] = max(counters[prefix], int(suffix))
    return counters


def _next_number(
    *,
    row: Mapping[str, Any],
    year: int | None = None,
    observed_counters: Mapping[str, int],
) -> str:
    number_year = int(year or date.today().year)
    last_year = row["last_counter_year"]
    current = int(row["last_counter"]) if last_year == number_year else 0
    prefix = _normalised_series_prefix(str(row["prefix"]), year=number_year)
    counter = max(current, int(observed_counters.get(prefix, 0)))
    counter = next_invoice_series_counter(counter)
    return _format_series_number(row, counter, year=number_year)


def _map_ids(db, *, subject_id: int, identities: set[str]) -> dict[tuple[str, str], int]:
    from sqlalchemy import select

    from fakturek.models import ImportMap

    out = {}
    for start in range(0, len(identities), 500):
        chunk = sorted(identities)[start : start + 500]
        for typ, external, internal in db.execute(
            select(ImportMap.entity_type, ImportMap.external_id, ImportMap.internal_id)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(
                ImportMap.entity_type.in_(
                    ("contact", "catalog_item", "bank_account", "invoice_series")
                )
            )
            .where(ImportMap.external_id.in_(chunk))
        ).all():
            out[(str(typ), str(external))] = int(internal)
    return out


def _incoming_series_names_collide_in_database(db, names: tuple[str, ...]) -> bool:
    """Ask MySQL/MariaDB to compare the complete incoming set with column collation."""
    if len(names) < 2 or db.get_bind().dialect.name not in {"mysql", "mariadb"}:
        return False

    from sqlalchemy import func, literal, select, text, union_all

    collation = db.scalar(
        text(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'invoice_series' AND COLUMN_NAME = 'name'"
        )
    )
    collation_name = str(collation or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", collation_name):
        raise ValueError("cannot validate invoice series names under database collation")

    # MAX_INVOICE_SERIES bounds this to at most 1,000 short SELECT terms and
    # one round-trip. Grouping the complete set is important: collisions must
    # not disappear across arbitrary batching boundaries.
    incoming = union_all(
        *(select(literal(name).collate(collation_name).label("name")) for name in names)
    ).subquery()
    collision = db.scalar(
        select(incoming.c.name).group_by(incoming.c.name).having(func.count() > 1).limit(1)
    )
    return collision is not None


def build_native_backup_v3_import_plan(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> ImportPlanV3:
    from sqlalchemy import select

    from fakturek.models import (
        Contact,
        InvoiceCatalogItem,
        InvoiceSeries,
        Subject,
        SubjectBankAccount,
    )

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")
    backup = _load_backup(
        run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
    )
    incoming_series_names = tuple(str(row["name"]) for row in backup.invoice_series)
    if _incoming_series_names_collide_in_database(db, incoming_series_names):
        raise ValueError("backup contains invoice series names that collide in the database")
    contacts = list(
        db.scalars(
            select(Contact).where(Contact.subject_id == int(subject_id)).order_by(Contact.id)
        ).all()
    )
    catalog = list(
        db.scalars(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .order_by(InvoiceCatalogItem.id)
        ).all()
    )
    accounts = list(
        db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.id)
        ).all()
    )
    series = list(
        db.scalars(
            select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == int(subject_id))
            .order_by(InvoiceSeries.id)
        ).all()
    )
    normalized_target_names = [str(x.name or "").casefold() for x in series]
    storage_target_names = [_series_storage_key(x.name) for x in series]
    if len(set(normalized_target_names)) != len(normalized_target_names):
        raise ValueError("target contains duplicate normalized invoice series names")
    if len(set(storage_target_names)) != len(storage_target_names):
        raise ValueError("target contains collation-conflicting invoice series names")
    mapped = _map_ids(
        db,
        subject_id=int(subject_id),
        identities={_digest(_canonical(r)) for r in backup.contacts + backup.catalog_items}
        | {_account_identity(r) for r in backup.bank_accounts}
        | {_series_identity(r) for r in backup.invoice_series},
    )
    indexes = _snapshot_indexes(contacts, catalog)
    account_index = {_stored_account_key(x): x for x in accounts}
    series_index = {str(x.name or "").casefold(): x for x in series}
    series_storage_index = {_series_storage_key(x.name): x for x in series}
    mode = _contact_mode(_run_config(run))
    warnings = []

    def actions(rows, entity, by_id, index, key, identity, allow_match=True):
        result = []
        seen = set()
        claimed = set()
        for row in rows:
            ident = identity(row)
            frozen = MappingProxyType(dict(row))
            if ident in seen:
                result.append(Action(ident, frozen, "reuse"))
                continue
            seen.add(ident)
            existing = by_id.get(mapped.get((entity, ident)))
            if (
                entity == "contact"
                and existing is not None
                and key(row) not in _stored_contact_keys(existing)
            ):
                raise ValueError("Native backup contact mapping no longer matches its identity")
            if (
                entity == "catalog_item"
                and existing is not None
                and _stored_catalog_signature(existing) != key(row)
            ):
                raise ValueError("Native backup catalog mapping no longer matches its identity")
            if (
                entity == "bank_account"
                and existing is not None
                and _stored_account_key(existing) != key(row)
            ):
                raise ValueError(
                    "Native backup bank-account mapping no longer matches its business identity"
                )
            if (
                entity == "invoice_series"
                and existing is not None
                and str(getattr(existing, "name", "") or "").casefold() != key(row)
            ):
                raise ValueError(
                    "Native backup invoice-series mapping no longer matches its business identity"
                )
            if existing is None and allow_match:
                candidate = index.get(key(row))
                if candidate is not None and int(candidate.id) not in claimed:
                    existing = candidate
                    claimed.add(int(candidate.id))
            result.append(
                Action(
                    ident,
                    frozen,
                    "reuse" if existing is not None else "create",
                    int(existing.id) if existing is not None else None,
                )
            )
        return result

    contact_actions = actions(
        backup.contacts,
        "contact",
        {int(x.id): x for x in contacts},
        indexes[0],
        _contact_match_key,
        lambda r: _digest(_canonical(r)),
        mode != "create_new",
    )
    catalog_actions = actions(
        backup.catalog_items,
        "catalog_item",
        {int(x.id): x for x in catalog},
        indexes[1],
        _catalog_signature,
        lambda r: _digest(_canonical(r)),
    )
    account_actions = actions(
        backup.bank_accounts,
        "bank_account",
        {int(x.id): x for x in accounts},
        account_index,
        _account_key,
        _account_identity,
    )
    series_actions = actions(
        backup.invoice_series,
        "invoice_series",
        {int(x.id): x for x in series},
        series_index,
        _series_key,
        _series_identity,
    )
    for action in series_actions:
        if (
            action.action == "create"
            and _series_storage_key(action.row["name"]) in series_storage_index
        ):
            raise ValueError("invoice series name conflicts under database collation")
    create_names = sorted(
        {str(action.row["name"]) for action in series_actions if action.action == "create"}
    )
    for start in range(0, len(create_names), 500):
        # Let the destination database apply its real collation too. This catches
        # equivalences beyond the conservative Unicode fold above without making
        # preview behavior depend on one SQL dialect's collation implementation.
        if (
            db.scalar(
                select(InvoiceSeries.id)
                .where(InvoiceSeries.subject_id == int(subject_id))
                .where(InvoiceSeries.name.in_(create_names[start : start + 500]))
                .limit(1)
            )
            is not None
        ):
            raise ValueError("invoice series name conflicts under database collation")
    if len(accounts) + sum(x.action == "create" for x in account_actions) > MAX_BANK_ACCOUNTS:
        raise ValueError("bank account limit would be exceeded")
    if len(series) + sum(x.action == "create" for x in series_actions) > MAX_INVOICE_SERIES:
        raise ValueError("invoice series limit would be exceeded")
    has_default = any(bool(x.is_default) for x in accounts)
    default_claimed = False
    checked = []
    for action in account_actions:
        wants = bool(action.row["is_default"])
        import_default = (
            wants and action.action == "create" and not has_default and not default_claimed
        )
        if import_default:
            default_claimed = True
        elif wants:
            warnings.append(
                "Importovaný výchozí účet se nenastaví, protože cílový subjekt "
                "už výchozí účet má nebo ho zachovává."
            )
        checked.append(
            Action(action.identity, action.row, action.action, action.existing_id, import_default)
        )
    series_by_id = {int(x.id): x for x in series}
    series_preview = []
    display_series = []
    for action in series_actions:
        if action.action == "reuse":
            target = series_by_id.get(int(action.existing_id or 0))
            if target is None:
                raise ValueError("Native backup invoice series changed during planning")
            display_row = _series_row_from_model(target)
        else:
            display_row = dict(action.row)
        display_series.append((action, display_row))
    number_year = date.today().year
    observed_counters = _observed_series_counters_for_year(
        db,
        subject_id=int(subject_id),
        prefixes={
            _normalised_series_prefix(str(row["prefix"]), year=number_year)
            for _action, row in display_series
        },
        year=number_year,
    )
    series_warnings = []
    for action, display_row in display_series:
        preview_row = {
            "name": str(display_row["name"])[:100],
            "action": action.action,
        }
        try:
            preview_row["next_number"] = _next_number(
                row=display_row,
                year=number_year,
                observed_counters=observed_counters,
            )[:50]
        except InvoiceSeriesCounterExhausted:
            preview_row["next_number"] = None
            preview_row["exhausted"] = True
            series_warnings.append(
                f"Číselná řada {display_row['name']} je pro rok {number_year} vyčerpaná; "
                "další číslo z ní nelze přidělit. Uložený stav se přesto bezpečně obnoví."
            )
        series_preview.append(preview_row)
    return ImportPlanV3(
        backup,
        mode,
        tuple(contact_actions),
        tuple(catalog_actions),
        tuple(checked),
        tuple(series_actions),
        tuple(dict.fromkeys(warnings)),
        tuple((int(x.id), _stored_contact_keys(x)) for x in contacts),
        tuple((int(x.id), _stored_catalog_signature(x)) for x in catalog),
        tuple(
            (int(x.id), _stored_account_key(x), bool(x.is_default), int(x.sort_order or 0))
            for x in accounts
        ),
        tuple(_stored_series_snapshot(x) for x in series),
        tuple(_freeze(row) for row in series_preview),
        tuple(series_warnings),
    )


def preview_native_backup_v3_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ImportPlanV3 | None = None,
) -> dict[str, Any]:
    plan = plan or build_native_backup_v3_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )

    def counts(items):
        return {
            "parsed": len(items),
            "will_create": sum(x.action == "create" for x in items),
            "will_reuse": sum(x.action == "reuse" for x in items),
        }

    return {
        "native_backup": True,
        "native_backup_v2": True,
        "native_backup_v3": True,
        "source": SOURCE,
        "ready_note": (
            "ZIP byl plně ověřen. Importuje pouze kontakty, katalog, bezpečná pole "
            "účtů a číselné řady; nikdy faktury, platby, tokeny, synchronizaci ani transakce."
        ),
        "manifest": _thaw(plan.backup.manifest),
        "datasets": {
            name: {"row_count": len(rows), "checksum": "ověřen"}
            for name, rows in (
                ("contacts", plan.contacts),
                ("catalog_items", plan.catalog_items),
                ("bank_accounts", plan.bank_accounts),
                ("invoice_series", plan.invoice_series),
            )
        },
        "contacts": counts(plan.contacts),
        "catalog_items": counts(plan.catalog_items),
        "bank_accounts": {
            **counts(plan.bank_accounts),
            "will_default": sum(x.import_default for x in plan.bank_accounts),
            "warnings": list(plan.warnings),
        },
        "invoice_series": {
            **counts(plan.invoice_series),
            "rows": [_thaw(row) for row in plan.series_preview],
            "warnings": list(plan.series_warnings),
        },
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


def _mapped_entity(db, *, subject_id: int, entity_type: str, identity: str, model):
    from sqlalchemy import select

    from fakturek.models import ImportMap

    mapped = db.scalar(
        select(ImportMap.internal_id)
        .where(ImportMap.subject_id == int(subject_id))
        .where(ImportMap.source == SOURCE)
        .where(ImportMap.entity_type == entity_type)
        .where(ImportMap.external_id == identity)
        .limit(1)
    )
    return (
        db.scalar(
            select(model)
            .where(model.subject_id == int(subject_id))
            .where(model.id == int(mapped))
            .limit(1)
        )
        if mapped is not None
        else None
    )


def _claim(db, *, subject_id: int, entity_type: str, identity: str, model, factory):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

    try:
        with db.begin_nested():
            entity = factory()
            db.add(entity)
            db.flush()
            db.add(
                ImportMap(
                    subject_id=int(subject_id),
                    source=SOURCE,
                    entity_type=entity_type,
                    external_id=identity,
                    internal_id=int(entity.id),
                )
            )
            db.flush()
        return entity, False
    except IntegrityError as collision:
        entity = _mapped_entity(
            db, subject_id=subject_id, entity_type=entity_type, identity=identity, model=model
        )
        if entity is not None:
            return entity, True

        # A prior restore may have been deliberately deleted while its audit map
        # remains. Reclaim that orphaned map atomically instead of making a saved
        # backup permanently unusable.
        mapping = db.scalar(
            select(ImportMap)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(ImportMap.entity_type == entity_type)
            .where(ImportMap.external_id == identity)
            .with_for_update()
            .limit(1)
        )
        if mapping is None:
            raise collision
        entity = db.scalar(
            select(model)
            .where(model.subject_id == int(subject_id))
            .where(model.id == int(mapping.internal_id))
            .limit(1)
        )
        if entity is not None:
            return entity, True
        with db.begin_nested():
            entity = factory()
            db.add(entity)
            db.flush()
            mapping.internal_id = int(entity.id)
            db.add(mapping)
            db.flush()
        return entity, False


def _bind(
    db,
    *,
    subject_id: int,
    entity_type: str,
    identity: str,
    entity,
    model,
    matches_identity,
):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

    if not matches_identity(entity):
        raise ValueError(f"Native backup {entity_type} no longer matches its identity")
    try:
        with db.begin_nested():
            db.add(
                ImportMap(
                    subject_id=int(subject_id),
                    source=SOURCE,
                    entity_type=entity_type,
                    external_id=identity,
                    internal_id=int(entity.id),
                )
            )
            db.flush()
        return entity, False
    except IntegrityError as collision:
        mapping = db.scalar(
            select(ImportMap)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(ImportMap.entity_type == entity_type)
            .where(ImportMap.external_id == identity)
            .with_for_update()
            .limit(1)
        )
        if mapping is None:
            raise collision
        winner = db.scalar(
            select(model)
            .where(model.subject_id == int(subject_id))
            .where(model.id == int(mapping.internal_id))
            .limit(1)
        )
        if winner is not None:
            if not matches_identity(winner):
                raise ValueError(
                    f"Native backup {entity_type} map race changed identity"
                ) from collision
            return winner, True

        # The mapped row was deleted, but planning found a safe manual
        # replacement. Retarget the audit map under lock instead of rejecting a
        # useful replay or creating a duplicate.
        with db.begin_nested():
            mapping.internal_id = int(entity.id)
            db.add(mapping)
            db.flush()
        return entity, False


def process_native_backup_v3_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ImportPlanV3 | None = None,
) -> dict[str, Any]:
    from decimal import Decimal

    from sqlalchemy import select

    from fakturek.models import Contact, InvoiceCatalogItem, InvoiceSeries, SubjectBankAccount

    plan = plan or build_native_backup_v3_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )
    lock_subject_bank_account_mutations(db, subject_id=int(subject_id))
    contacts = list(
        db.scalars(
            select(Contact)
            .where(Contact.subject_id == int(subject_id))
            .order_by(Contact.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    catalog = list(
        db.scalars(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .order_by(InvoiceCatalogItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    accounts = list(
        db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    series = list(
        db.scalars(
            select(InvoiceSeries)
            .where(InvoiceSeries.subject_id == int(subject_id))
            .order_by(InvoiceSeries.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if tuple((int(x.id), _stored_contact_keys(x)) for x in contacts) != plan.contact_snapshot:
        raise ValueError("Contact destination changed since preview; import was not applied")
    if tuple((int(x.id), _stored_catalog_signature(x)) for x in catalog) != plan.catalog_snapshot:
        raise ValueError("Catalog destination changed since preview; import was not applied")
    if (
        tuple(
            (int(x.id), _stored_account_key(x), bool(x.is_default), int(x.sort_order or 0))
            for x in accounts
        )
        != plan.account_snapshot
    ):
        raise ValueError("Bank account destination changed since preview; import was not applied")
    if tuple(_stored_series_snapshot(x) for x in series) != plan.series_snapshot:
        raise ValueError("Invoice series destination changed since preview; import was not applied")
    summary = {
        "phase": "native_backup_v3",
        "source": SOURCE,
        "backup": {
            "version": VERSION,
            "datasets": _thaw(plan.backup.manifest["datasets"]),
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
        "bank_accounts": {
            "parsed": len(plan.bank_accounts),
            "created": 0,
            "reused": 0,
            "default_applied": 0,
            "warnings": list(plan.warnings),
        },
        "invoice_series": {
            "parsed": len(plan.invoice_series),
            "created": 0,
            "reused": 0,
            "unchanged": 0,
        },
    }

    def apply(items, typ, model, factory, count, matches_identity):
        resolved = {}
        for action in items:
            if action.identity in resolved:
                count["reused"] += 1
                continue
            if action.action == "reuse":
                entity = db.scalar(
                    select(model)
                    .where(model.subject_id == int(subject_id))
                    .where(model.id == action.existing_id)
                    .limit(1)
                )
                if entity is None:
                    raise ValueError("Native backup record changed during processing")
                entity, _ = _bind(
                    db,
                    subject_id=subject_id,
                    entity_type=typ,
                    identity=action.identity,
                    entity=entity,
                    model=model,
                    matches_identity=lambda candidate, action=action: matches_identity(
                        candidate, action.row
                    ),
                )
                if not matches_identity(entity, action.row):
                    raise ValueError(f"Native backup {typ} map race changed identity")
                count["reused"] += 1
                if typ == "contact" and plan.contact_mode == "merge_existing":
                    for field in CONTACT_FIELDS[1:]:
                        if (
                            field != "registry_auto_update"
                            and action.row.get(field) is not None
                            and not getattr(entity, field, None)
                        ):
                            setattr(entity, field, action.row[field])
                    db.add(entity)
                elif typ == "contact" and plan.contact_mode == "skip_existing":
                    count["skipped_existing"] += 1
            else:
                entity, lost = _claim(
                    db,
                    subject_id=subject_id,
                    entity_type=typ,
                    identity=action.identity,
                    model=model,
                    factory=lambda action=action: factory(action),
                )
                if not matches_identity(entity, action.row):
                    raise ValueError(f"Native backup {typ} map race changed identity")
                count["reused" if lost else "created"] += 1
            resolved[action.identity] = entity

    apply(
        plan.contacts,
        "contact",
        Contact,
        lambda a: Contact(
            subject_id=int(subject_id),
            name=a.row["name"],
            **{f: a.row[f] for f in CONTACT_FIELDS[1:] if f in a.row},
        ),
        summary["contacts"],
        lambda entity, row: _contact_match_key(row) in _stored_contact_keys(entity),
    )
    apply(
        plan.catalog_items,
        "catalog_item",
        InvoiceCatalogItem,
        lambda a: InvoiceCatalogItem(
            subject_id=int(subject_id),
            description=a.row["description"],
            quantity=Decimal(a.row["quantity"]),
            unit=a.row["unit"],
            unit_price_cents=int(a.row["unit_price_cents"]),
            vat_rate=Decimal(a.row["vat_rate"]),
            currency=a.row["currency"],
        ),
        summary["catalog_items"],
        lambda entity, row: _stored_catalog_signature(entity) == _catalog_signature(row),
    )
    accounts_by_id = {int(x.id): x for x in accounts}
    next_order = max((int(x.sort_order or 0) for x in accounts), default=-1) + 1
    has_default = any(bool(x.is_default) for x in accounts)
    for action in sorted(plan.bank_accounts, key=lambda a: (a.row["sort_order"], a.identity)):
        if action.action == "reuse":
            entity = accounts_by_id.get(int(action.existing_id or 0))
            if entity is None:
                raise ValueError("Native backup bank account changed during processing")
            entity, _ = _bind(
                db,
                subject_id=subject_id,
                entity_type="bank_account",
                identity=action.identity,
                entity=entity,
                model=SubjectBankAccount,
                matches_identity=lambda candidate, action=action: (
                    _stored_account_key(candidate) == _account_key(action.row)
                ),
            )
            if _stored_account_key(entity) != _account_key(action.row):
                raise ValueError("Native backup bank-account map race changed identity")
            summary["bank_accounts"]["reused"] += 1
            continue
        assigned = next_order
        next_order += 1
        r = action.row
        entity, lost = _claim(
            db,
            subject_id=subject_id,
            entity_type="bank_account",
            identity=action.identity,
            model=SubjectBankAccount,
            factory=lambda r=r, assigned=assigned: SubjectBankAccount(
                subject_id=int(subject_id),
                label=r["label"],
                account_number=r["account_number"],
                iban=r["iban"] or None,
                bic=r["bic"] or None,
                country=r["country"],
                currency=r["currency"],
                is_default=False,
                sort_order=assigned,
            ),
        )
        if _stored_account_key(entity) != _account_key(action.row):
            raise ValueError("Native backup bank-account map race changed identity")
        summary["bank_accounts"]["reused" if lost else "created"] += 1
        if action.import_default and not lost and not has_default:
            entity.is_default = True
            db.add(entity)
            has_default = True
            summary["bank_accounts"]["default_applied"] += 1
    current_series_by_id = {int(x.id): x for x in series}
    for action in plan.invoice_series:
        if action.action == "reuse":
            entity = current_series_by_id.get(int(action.existing_id or 0))
            if entity is None or str(entity.name or "").casefold() != _series_key(action.row):
                raise ValueError("Native backup invoice series changed during processing")
            _bind(
                db,
                subject_id=subject_id,
                entity_type="invoice_series",
                identity=action.identity,
                entity=entity,
                model=InvoiceSeries,
                matches_identity=lambda candidate, action=action: (
                    str(candidate.name or "").casefold() == _series_key(action.row)
                ),
            )
            summary["invoice_series"]["reused"] += 1
            summary["invoice_series"]["unchanged"] += 1
            continue
        r = action.row
        entity, lost = _claim(
            db,
            subject_id=subject_id,
            entity_type="invoice_series",
            identity=action.identity,
            model=InvoiceSeries,
            factory=lambda r=r: InvoiceSeries(
                subject_id=int(subject_id),
                name=r["name"],
                prefix=r["prefix"],
                pad_length=int(r["pad_length"]),
                last_counter=int(r["last_counter"]),
                last_counter_year=r["last_counter_year"],
            ),
        )
        if str(entity.name or "").casefold() != _series_key(r):
            raise ValueError("Native backup invoice series map race changed identity")
        summary["invoice_series"]["reused" if lost else "created"] += 1
    summary["note"] = (
        f"contacts: +{summary['contacts']['created']}; "
        f"catalog items: +{summary['catalog_items']['created']}; "
        f"bank accounts: +{summary['bank_accounts']['created']}; "
        f"invoice series: +{summary['invoice_series']['created']}"
    )
    return summary

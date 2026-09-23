"""Strict Fakturek-native backup v2.

V2 is intentionally a new format and source.  It carries the portable v1
master data plus a deliberately small bank-account projection; credentials,
sync state and transaction data never enter this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from fakturek.bank_account_lock import lock_subject_bank_account_mutations
from fakturek.banking import normalize_bic, normalize_iban
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

FORMAT = "fakturek-native-backup"
VERSION = 2
SOURCE = "fakturek_native_v2"
MEDIA_TYPE = "application/vnd.fakturek.native-backup-v2+zip"
MEMBERS = (
    "manifest.json",
    "contacts.jsonl",
    "catalog_items.jsonl",
    "bank_accounts.jsonl",
)
SCHEMA_VERSION = 1
MAX_ROWS_PER_DATASET = 100_000
MAX_BANK_ACCOUNTS = 1_000
BANK_ACCOUNT_FIELDS = (
    "label",
    "account_number",
    "iban",
    "bic",
    "country",
    "currency",
    "is_default",
    "sort_order",
)


@dataclass(frozen=True)
class BackupV2:
    manifest: Mapping[str, Any]
    contacts: tuple[Mapping[str, Any], ...]
    catalog_items: tuple[Mapping[str, Any], ...]
    bank_accounts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Action:
    identity: str
    row: Mapping[str, Any]
    action: str
    existing_id: int | None = None
    import_default: bool = False


@dataclass(frozen=True)
class ImportPlanV2:
    backup: BackupV2
    contact_mode: str
    contacts: tuple[Action, ...]
    catalog_items: tuple[Action, ...]
    bank_accounts: tuple[Action, ...]
    warnings: tuple[str, ...]
    account_snapshot: tuple[tuple[int, tuple[str, ...], bool, int], ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        _thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _freeze(value: Any) -> Any:
    """Recursively freeze parsed backup data before exposing it in a plan."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return JSON-compatible copies for API preview/summary responses."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def row_identity(row: dict[str, Any]) -> str:
    return _digest(_canonical(row))


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical(row) + b"\n" for row in rows)


def _text(value: object, *, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"bank account {name} must be a string")
    result = value.strip()
    if (required and not result) or len(result) > maximum:
        raise ValueError(f"bank account {name} is invalid")
    return result


def _normalise_bank_account(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(BANK_ACCOUNT_FIELDS):
        raise ValueError("bank account row must contain exactly the supported fields")
    label = _text(raw["label"], name="label", maximum=120)
    number = _text(raw["account_number"], name="account_number", maximum=255)
    iban = _text(raw["iban"], name="iban", maximum=34)
    bic = _text(raw["bic"], name="bic", maximum=11)
    country = _text(raw["country"], name="country", maximum=2, required=True).upper()
    currency = _text(raw["currency"], name="currency", maximum=3, required=True).upper()
    if not re.fullmatch(r"[A-Z]{2}", country) or not re.fullmatch(
        r"[A-Z]{3}", currency
    ):
        raise ValueError("bank account country or currency is invalid")
    # Storage uses normalized forms.  Do not accept an arbitrary token-like
    # string in an account identity field.
    number = re.sub(r"\s+", "", number)
    if not iban and re.fullmatch(r"[A-Za-z]{2}[0-9]{2}[A-Za-z0-9]{10,30}", number):
        iban = number
        number = ""
    iban = normalize_iban(iban) if iban else ""
    bic = normalize_bic(bic) if bic else ""
    if not number and not iban:
        raise ValueError("bank account number or IBAN is required")
    if iban and iban[:2] != country:
        raise ValueError("bank account country does not match IBAN")
    if not isinstance(raw["is_default"], bool):
        raise ValueError("bank account is_default must be a boolean")
    if (
        not isinstance(raw["sort_order"], int)
        or isinstance(raw["sort_order"], bool)
        or not 0 <= raw["sort_order"] <= 1_000_000
    ):
        raise ValueError("bank account sort_order is invalid")
    return {
        "label": label,
        "account_number": number,
        "iban": iban,
        "bic": bic,
        "country": country,
        "currency": currency,
        "is_default": raw["is_default"],
        "sort_order": raw["sort_order"],
    }


def _account_export_row(account: object) -> dict[str, Any]:
    # This literal projection is a security boundary.  In particular, do not
    # expand it with attributes of SubjectBankAccount.
    return _normalise_bank_account(
        {
            "label": str(getattr(account, "label", "") or ""),
            "account_number": str(getattr(account, "account_number", "") or ""),
            "iban": str(getattr(account, "iban", "") or ""),
            "bic": str(getattr(account, "bic", "") or ""),
            "country": str(getattr(account, "country", "") or ""),
            "currency": str(getattr(account, "currency", "") or ""),
            "is_default": bool(getattr(account, "is_default", False)),
            "sort_order": int(getattr(account, "sort_order", 0) or 0),
        }
    )


def _contact_export_row(contact: object) -> dict[str, Any]:
    row: dict[str, Any] = {"name": str(getattr(contact, "name", "") or "").strip()}
    for field in CONTACT_FIELDS[1:]:
        value = getattr(contact, field, None)
        if value is not None:
            row[field] = (
                bool(value) if field == "registry_auto_update" else str(value).strip()
            )
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


def build_native_backup_v2_bytes(
    *,
    contacts: list[object],
    catalog_items: list[object],
    bank_accounts: list[object],
    max_rows: int = MAX_ROWS_PER_DATASET,
    max_member_bytes: int,
    max_archive_bytes: int,
) -> bytes:
    if (
        len(contacts) > max_rows
        or len(catalog_items) > max_rows
        or len(bank_accounts) > MAX_BANK_ACCOUNTS
    ):
        raise ValueError("Native backup v2 dataset exceeds its row limit")
    account_rows = sorted(
        (_account_export_row(x) for x in bank_accounts),
        key=lambda r: (r["sort_order"], _canonical(r)),
    )
    account_keys = [_account_key(row) for row in account_rows]
    if len(set(account_keys)) != len(account_keys):
        raise ValueError("Native backup v2 contains duplicate bank account identities")
    if sum(bool(row["is_default"]) for row in account_rows) > 1:
        raise ValueError("Native backup v2 contains more than one default bank account")
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
        ("bank_accounts", "bank_accounts.jsonl", account_rows),
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
        raise ValueError(
            "Native backup v2 dataset exceeds the configured import size limit"
        )
    out = io.BytesIO()
    with zipfile.ZipFile(
        out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for filename in MEMBERS:
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content[filename])
    payload = out.getvalue()
    if len(payload) > max_archive_bytes:
        raise ValueError(
            "Native backup v2 exceeds the configured import upload size limit"
        )
    return payload


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) == 1
    )


def _read_jsonl(
    data: bytes, *, normalizer, row_limit: int, label: str
) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc
    if text and not text.endswith("\n"):
        raise ValueError(f"{label} must end with a newline")
    rows = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{label} has an empty row")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has malformed JSONL at row {line_no}") from exc
        rows.append(normalizer(row))
        if len(rows) > row_limit:
            raise ValueError(f"{label} has too many rows")
    return rows


def parse_native_backup_v2_bytes(
    data: bytes,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    max_rows: int = MAX_ROWS_PER_DATASET,
) -> BackupV2:
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
            raise ValueError("backup v2 must contain exactly its declared members")
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
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {"format", "version", "generated_at_utc", "export_id", "datasets"}
        or manifest.get("format") != FORMAT
        or manifest.get("version") != VERSION
    ):
        raise ValueError("backup format, version or manifest schema is unsupported")
    try:
        if str(UUID(manifest.get("export_id"))) != manifest["export_id"]:
            raise ValueError
        stamp = datetime.fromisoformat(
            str(manifest["generated_at_utc"]).replace("Z", "+00:00")
        )
        if stamp.tzinfo is None or stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("backup manifest identity or timestamp is invalid") from exc
    expected = {
        "contacts": ("contacts.jsonl", _normalise_contact, max_rows),
        "catalog_items": ("catalog_items.jsonl", _normalise_catalog_item, max_rows),
        "bank_accounts": (
            "bank_accounts.jsonl",
            _normalise_bank_account,
            MAX_BANK_ACCOUNTS,
        ),
    }
    entries = manifest.get("datasets")
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("backup manifest datasets are invalid")
    by_name = {x.get("name"): x for x in entries if isinstance(x, dict)}
    if set(by_name) != set(expected):
        raise ValueError("backup manifest datasets are invalid")
    parsed = {}
    for name, (filename, normalizer, limit) in expected.items():
        entry = by_name[name]
        if (
            set(entry) != {"name", "filename", "schema_version", "row_count", "sha256"}
            or entry.get("filename") != filename
            or entry.get("schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("backup manifest dataset schema is invalid")
        if (
            not isinstance(entry.get("row_count"), int)
            or isinstance(entry["row_count"], bool)
            or not 0 <= entry["row_count"] <= limit
        ):
            raise ValueError("backup manifest row count is invalid")
        if (
            not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
            or entry["sha256"].lower() != _digest(content[filename])
        ):
            raise ValueError("backup dataset checksum does not match")
        parsed[name] = _read_jsonl(
            content[filename], normalizer=normalizer, row_limit=limit, label=filename
        )
        if len(parsed[name]) != entry["row_count"]:
            raise ValueError("backup dataset row count does not match")
    account_keys = [_account_key(row) for row in parsed["bank_accounts"]]
    if len(set(account_keys)) != len(account_keys):
        raise ValueError("backup contains duplicate bank account identities")
    if sum(bool(row["is_default"]) for row in parsed["bank_accounts"]) > 1:
        raise ValueError("backup contains more than one default bank account")
    return BackupV2(
        _freeze(manifest),
        tuple(_freeze(row) for row in parsed["contacts"]),
        tuple(_freeze(row) for row in parsed["catalog_items"]),
        tuple(_freeze(row) for row in parsed["bank_accounts"]),
    )


def _load_backup(
    run: object, *, import_storage_root: Path, max_upload_bytes: int
) -> BackupV2:
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
    return parse_native_backup_v2_bytes(
        payload,
        max_member_bytes=max_upload_bytes,
        max_total_bytes=max_upload_bytes * len(MEMBERS),
    )


def _account_key(row: dict[str, Any]) -> tuple[str, ...]:
    if row["iban"]:
        return ("iban", row["iban"])
    return ("number", row["account_number"].casefold(), row["country"], row["currency"])


def _account_identity(row: dict[str, Any]) -> str:
    """Map the stable account business identity, never presentational fields."""
    return _digest(_canonical(_account_key(row)))


def _stored_account_key(account: object) -> tuple[str, ...]:
    iban = re.sub(r"\s+", "", str(getattr(account, "iban", "") or "")).upper()
    if iban:
        return ("iban", iban)
    return (
        "number",
        re.sub(
            r"\s+", "", str(getattr(account, "account_number", "") or "")
        ).casefold(),
        str(getattr(account, "country", "") or "").upper(),
        str(getattr(account, "currency", "") or "").upper(),
    )


def _map_ids(
    db, *, subject_id: int, identities: set[str]
) -> dict[tuple[str, str], int]:
    from sqlalchemy import select

    from fakturek.models import ImportMap

    out = {}
    for start in range(0, len(identities), 500):
        chunk = sorted(identities)[start : start + 500]
        for entity_type, external_id, internal_id in db.execute(
            select(ImportMap.entity_type, ImportMap.external_id, ImportMap.internal_id)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(
                ImportMap.entity_type.in_(("contact", "catalog_item", "bank_account"))
            )
            .where(ImportMap.external_id.in_(chunk))
        ).all():
            out[(str(entity_type), str(external_id))] = int(internal_id)
    return out


def build_native_backup_v2_import_plan(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
) -> ImportPlanV2:
    from sqlalchemy import select

    from fakturek.models import Contact, InvoiceCatalogItem, Subject, SubjectBankAccount

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if (
        db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1))
        is None
    ):
        raise ValueError("Subject does not exist")
    backup = _load_backup(
        run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
    )
    contacts = list(
        db.scalars(
            select(Contact)
            .where(Contact.subject_id == int(subject_id))
            .order_by(Contact.id)
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
    mapped = _map_ids(
        db,
        subject_id=int(subject_id),
        identities={row_identity(r) for r in backup.contacts + backup.catalog_items}
        | {_account_identity(r) for r in backup.bank_accounts},
    )
    contact_by_id, catalog_by_id, account_by_id = (
        {int(x.id): x for x in rows} for rows in (contacts, catalog, accounts)
    )
    contact_index, catalog_index = _snapshot_indexes(contacts, catalog)
    account_index = {}
    for item in accounts:
        account_index.setdefault(_stored_account_key(item), item)
    mode = _contact_mode(_run_config(run))
    warnings = []

    def actions(
        rows, entity, by_id, index, key, identity_key=row_identity, allow_match=True
    ):
        result = []
        seen = set()
        claimed = set()
        for row in rows:
            identity = identity_key(row)
            frozen_row = MappingProxyType(dict(row))
            if identity in seen:
                result.append(Action(identity, frozen_row, "reuse"))
                continue
            seen.add(identity)
            existing = by_id.get(mapped.get((entity, identity)))
            if (
                entity == "bank_account"
                and existing is not None
                and _stored_account_key(existing) != key(row)
            ):
                raise ValueError(
                    "Native backup bank-account mapping no longer matches its business identity"
                )
            if existing is None and allow_match:
                candidate = index.get(key(row))
                if candidate is not None and int(candidate.id) not in claimed:
                    existing = candidate
                    claimed.add(int(candidate.id))
            result.append(
                Action(
                    identity,
                    frozen_row,
                    "reuse" if existing is not None else "create",
                    int(existing.id) if existing is not None else None,
                )
            )
        return result

    contact_actions = actions(
        backup.contacts,
        "contact",
        contact_by_id,
        contact_index,
        _contact_match_key,
        allow_match=mode != "create_new",
    )
    catalog_actions = actions(
        backup.catalog_items,
        "catalog_item",
        catalog_by_id,
        catalog_index,
        _catalog_signature,
    )
    account_actions = actions(
        backup.bank_accounts,
        "bank_account",
        account_by_id,
        account_index,
        _account_key,
        _account_identity,
    )
    creates = [x for x in account_actions if x.action == "create"]
    if len(accounts) + len(creates) > MAX_BANK_ACCOUNTS:
        raise ValueError("bank account limit would be exceeded")
    has_default = any(bool(x.is_default) for x in accounts)
    default_claimed = False
    account_actions2 = []
    for action in account_actions:
        wants = bool(action.row["is_default"])
        import_default = False
        if (
            wants
            and action.action == "create"
            and not has_default
            and not default_claimed
        ):
            import_default = True
            default_claimed = True
        elif wants and (has_default or action.action == "reuse" or default_claimed):
            warnings.append(
                "Importovaný výchozí účet se nenastaví, protože cílový subjekt už "
                "výchozí účet má nebo ho zachovává."
            )
        account_actions2.append(
            Action(
                action.identity,
                action.row,
                action.action,
                action.existing_id,
                import_default,
            )
        )
    snapshot = tuple(
        (
            int(item.id),
            _stored_account_key(item),
            bool(item.is_default),
            int(item.sort_order or 0),
        )
        for item in accounts
    )
    return ImportPlanV2(
        backup,
        mode,
        tuple(contact_actions),
        tuple(catalog_actions),
        tuple(account_actions2),
        tuple(dict.fromkeys(warnings)),
        snapshot,
    )


def preview_native_backup_v2_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ImportPlanV2 | None = None,
) -> dict[str, Any]:
    plan = plan or build_native_backup_v2_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )

    def counts(actions):
        return {
            "parsed": len(actions),
            "will_create": sum(a.action == "create" for a in actions),
            "will_reuse": sum(a.action == "reuse" for a in actions),
        }

    return {
        "native_backup": True,
        "native_backup_v2": True,
        "source": SOURCE,
        "ready_note": (
            "ZIP byl plně ověřen. Importuje pouze kontakty, katalog a bezpečná "
            "pole bankovních účtů; nikdy tokeny, synchronizaci ani transakce."
        ),
        "manifest": _thaw(plan.backup.manifest),
        "datasets": {
            name: {"row_count": len(rows), "checksum": "ověřen"}
            for name, rows in (
                ("contacts", plan.contacts),
                ("catalog_items", plan.catalog_items),
                ("bank_accounts", plan.bank_accounts),
            )
        },
        "contacts": counts(plan.contacts),
        "catalog_items": counts(plan.catalog_items),
        "bank_accounts": {
            **counts(plan.bank_accounts),
            "will_default": sum(a.import_default for a in plan.bank_accounts),
            "warnings": list(plan.warnings),
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
    except IntegrityError:
        entity = _mapped_entity(
            db,
            subject_id=subject_id,
            entity_type=entity_type,
            identity=identity,
            model=model,
        )
        if entity is None:
            raise
        return entity, True


def _bind(db, *, subject_id: int, entity_type: str, identity: str, entity, model):
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

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
    except IntegrityError:
        winner = _mapped_entity(
            db,
            subject_id=subject_id,
            entity_type=entity_type,
            identity=identity,
            model=model,
        )
        if winner is None:
            raise
        return winner, True


def process_native_backup_v2_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ImportPlanV2 | None = None,
) -> dict[str, Any]:
    from decimal import Decimal

    from sqlalchemy import select

    from fakturek.models import Contact, InvoiceCatalogItem, SubjectBankAccount

    plan = plan or build_native_backup_v2_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )
    lock_subject_bank_account_mutations(db, subject_id=int(subject_id))
    current_accounts = list(
        db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    current_snapshot = tuple(
        (
            int(item.id),
            _stored_account_key(item),
            bool(item.is_default),
            int(item.sort_order or 0),
        )
        for item in current_accounts
    )
    if current_snapshot != plan.account_snapshot:
        raise ValueError(
            "Bank account destination changed since preview; import was not applied"
        )
    summary = {
        "phase": "native_backup_v2",
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
    }

    def apply(actions, entity_type, model, factory, count):
        resolved = {}
        for action in actions:
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
                    entity_type=entity_type,
                    identity=action.identity,
                    entity=entity,
                    model=model,
                )
                count["reused"] += 1
                if entity_type == "contact":
                    if plan.contact_mode == "skip_existing":
                        count["skipped_existing"] += 1
                    elif plan.contact_mode == "merge_existing":
                        for field in CONTACT_FIELDS[1:]:
                            if (
                                field != "registry_auto_update"
                                and action.row.get(field) is not None
                                and not getattr(entity, field, None)
                            ):
                                setattr(entity, field, action.row[field])
                        db.add(entity)
            else:
                entity, lost = _claim(
                    db,
                    subject_id=subject_id,
                    entity_type=entity_type,
                    identity=action.identity,
                    model=model,
                    factory=lambda action=action: factory(action),
                )
                count["reused" if lost else "created"] += 1
            resolved[action.identity] = entity
        return resolved

    def make_contact(a):
        row = a.row
        value = Contact(subject_id=int(subject_id), name=row["name"])
        for field in CONTACT_FIELDS[1:]:
            if field in row:
                setattr(value, field, row[field])
        return value

    def make_catalog(a):
        r = a.row
        return InvoiceCatalogItem(
            subject_id=int(subject_id),
            description=r["description"],
            quantity=Decimal(r["quantity"]),
            unit=r["unit"],
            unit_price_cents=int(r["unit_price_cents"]),
            vat_rate=Decimal(r["vat_rate"]),
            currency=r["currency"],
        )

    apply(plan.contacts, "contact", Contact, make_contact, summary["contacts"])
    apply(
        plan.catalog_items,
        "catalog_item",
        InvoiceCatalogItem,
        make_catalog,
        summary["catalog_items"],
    )
    current_accounts_by_id = {int(account.id): account for account in current_accounts}
    next_order = max(
        (int(account.sort_order or 0) for account in current_accounts),
        default=-1,
    ) + 1
    has_actual_default = any(bool(account.is_default) for account in current_accounts)
    ordered = sorted(
        plan.bank_accounts, key=lambda a: (a.row["sort_order"], a.identity)
    )
    for action in ordered:
        if action.action == "reuse":
            entity = current_accounts_by_id.get(int(action.existing_id or 0))
            if entity is None:
                raise ValueError("Native backup bank account changed during processing")
            entity, _ = _bind(
                db,
                subject_id=subject_id,
                entity_type="bank_account",
                identity=action.identity,
                entity=entity,
                model=SubjectBankAccount,
            )
            if _stored_account_key(entity) != _account_key(action.row):
                raise ValueError("Native backup bank-account map race changed identity")
            summary["bank_accounts"]["reused"] += 1
            continue
        assigned = next_order
        next_order += 1

        def make_account(action=action, assigned=assigned):
            r = action.row
            return SubjectBankAccount(
                subject_id=int(subject_id),
                label=r["label"],
                account_number=r["account_number"],
                iban=r["iban"] or None,
                bic=r["bic"] or None,
                country=r["country"],
                currency=r["currency"],
                is_default=False,
                sort_order=assigned,
            )

        entity, lost = _claim(
            db,
            subject_id=subject_id,
            entity_type="bank_account",
            identity=action.identity,
            model=SubjectBankAccount,
            factory=make_account,
        )
        if _stored_account_key(entity) != _account_key(action.row):
            raise ValueError("Native backup bank-account map race changed identity")
        summary["bank_accounts"]["reused" if lost else "created"] += 1
        if action.import_default and not lost:
            if not has_actual_default:
                entity.is_default = True
                db.add(entity)
                has_actual_default = True
                summary["bank_accounts"]["default_applied"] += 1
            else:
                summary["bank_accounts"]["warnings"].append(
                    "Importovaný výchozí účet se nenastaví, protože cílový subjekt "
                    "už výchozí účet má."
                )
    summary["note"] = (
        f"contacts: +{summary['contacts']['created']}; "
        f"catalog items: +{summary['catalog_items']['created']}; "
        f"bank accounts: +{summary['bank_accounts']['created']}"
    )
    return summary

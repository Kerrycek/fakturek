"""Strict, portable Fakturek contacts CSV v1 encoding.

The wire format contains no database identifiers. The import lifecycle keeps
its deterministic identities in a source-specific audit namespace and never
overwrites an existing contact.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fakturek.security import csv_safe_cell

SOURCE = "fakturek_contacts_csv_v1"
VERSION = "1"
HEADER = (
    "fakturek_contacts_csv_version",
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
)
BUSINESS_FIELDS = HEADER[1:]
FIELD_LIMITS = {
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
}
MAX_ROWS = 100_000
MAX_CELL_CHARS = 4_096
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_DB_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class ContactsCsvRow:
    name: str
    email: str
    phone: str
    street: str
    city: str
    zip: str
    country: str
    ico: str
    dic: str
    fixed_variable_symbol: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContactsCsvAction:
    identity: str
    row: ContactsCsvRow
    action: str
    existing_id: int | None = None
    map_known: bool = False
    map_orphan: bool = False


@dataclass(frozen=True, slots=True)
class ContactsCsvPlan:
    run_id: int
    subject_id: int
    source: str
    file_sha256: str
    file_size_bytes: int
    rows: tuple[ContactsCsvRow, ...]
    actions: tuple[ContactsCsvAction, ...]
    contact_snapshot: tuple[tuple[int, tuple[str, ...]], ...]
    map_snapshot: tuple[tuple[str, int], ...]


def _formula_export(value: str) -> str:
    """Escape spreadsheet formulas without losing a genuine apostrophe."""

    return csv_safe_cell("'" + value if value.startswith("'") else value)


def _formula_import(value: str) -> str:
    if value.startswith("''"):
        return value[1:]
    if value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES):
        return value[1:]
    return value


def _clean_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool = False,
    decode_formula: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if len(value) > MAX_CELL_CHARS:
        raise ValueError(f"{field} cell is too large")
    result = _formula_import(value) if decode_formula else value
    if "\x00" in result:
        raise ValueError(f"{field} contains an invalid character")
    if required and not result.strip():
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def normalise_row(
    raw: Mapping[str, object],
    *,
    decode_formula: bool = True,
) -> ContactsCsvRow:
    if set(raw) != set(HEADER):
        raise ValueError("contacts CSV row has an unsupported schema")
    if raw[HEADER[0]] != VERSION:
        raise ValueError("contacts CSV version is unsupported")
    values = {
        field: _clean_text(
            raw[field],
            field=field,
            maximum=FIELD_LIMITS[field],
            required=field == "name",
            decode_formula=decode_formula,
        )
        for field in BUSINESS_FIELDS
    }
    return ContactsCsvRow(**values)


def row_identity(row: ContactsCsvRow) -> str:
    """Return a stable, tenant-independent digest of the complete row."""

    canonical = json.dumps(
        row.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_from_contact(contact: object) -> ContactsCsvRow:
    raw: dict[str, object] = {HEADER[0]: VERSION}
    for field in BUSINESS_FIELDS:
        raw[field] = str(getattr(contact, field, "") or "")
    return normalise_row(raw, decode_formula=False)


def build_contacts_csv_bytes(
    *,
    contacts: list[object],
    max_rows: int = MAX_ROWS,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> bytes:
    max_upload_bytes = min(int(max_upload_bytes), MAX_UPLOAD_BYTES)
    if max_rows < 0 or max_upload_bytes < 1:
        raise ValueError("contacts CSV limits are invalid")
    if len(contacts) > max_rows:
        raise ValueError(f"Contacts CSV supports at most {max_rows} rows")
    rows = sorted((_row_from_contact(contact) for contact in contacts), key=row_identity)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    writer.writerow(HEADER)
    for row in rows:
        values = row.as_dict()
        writer.writerow((VERSION, *(_formula_export(values[field]) for field in BUSINESS_FIELDS)))
    payload = b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")
    if len(payload) > max_upload_bytes:
        raise ValueError("contacts CSV is too large")
    return payload


def parse_contacts_csv_bytes(
    data: bytes,
    *,
    max_rows: int = MAX_ROWS,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> list[ContactsCsvRow]:
    max_upload_bytes = min(int(max_upload_bytes), MAX_UPLOAD_BYTES)
    if max_rows < 0 or max_upload_bytes < 1:
        raise ValueError("contacts CSV limits are invalid")
    if not data:
        raise ValueError("contacts CSV is empty")
    if len(data) > max_upload_bytes:
        raise ValueError("contacts CSV is too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("contacts CSV is not UTF-8") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=";", strict=True)
        if next(reader, None) != list(HEADER):
            raise ValueError("contacts CSV header must exactly match v1")
        rows: list[ContactsCsvRow] = []
        trailing_blank = False
        for line_no, fields in enumerate(reader, start=2):
            if not fields or all(not value.strip() for value in fields):
                trailing_blank = True
                continue
            if trailing_blank:
                raise ValueError(
                    f"contacts CSV has a non-blank row after trailing blank row {line_no}"
                )
            if len(fields) != len(HEADER):
                raise ValueError(f"contacts CSV has an invalid column count at row {line_no}")
            if any(len(value) > MAX_CELL_CHARS for value in fields):
                raise ValueError(f"contacts CSV has an oversized cell at row {line_no}")
            rows.append(
                normalise_row({field: fields[index] for index, field in enumerate(HEADER)})
            )
            if len(rows) > max_rows:
                raise ValueError(f"Contacts CSV supports at most {max_rows} rows")
    except csv.Error as exc:
        raise ValueError("contacts CSV is malformed") from exc
    return rows


def _load_payload(run: object, *, import_storage_root: Path, max_upload_bytes: int) -> bytes:
    relative = str(getattr(run, "file_path", "") or "").strip()
    root = import_storage_root.resolve()
    target = (root / relative).resolve()
    if not relative or root not in target.parents or not target.is_file():
        raise ValueError("Import file is unavailable")
    data = target.read_bytes()
    if len(data) > min(int(max_upload_bytes), MAX_UPLOAD_BYTES):
        raise ValueError("contacts CSV is too large")
    expected = str(getattr(run, "file_sha256", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("stored contacts CSV checksum is invalid")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("stored contacts CSV checksum does not match")
    declared_size = int(getattr(run, "file_size_bytes", -1) or 0)
    if declared_size != len(data):
        raise ValueError("stored contacts CSV size does not match")
    return data


def _stored_row(contact: object) -> ContactsCsvRow:
    return _row_from_contact(contact)


def _stored_signature(contact: object) -> tuple[str, ...]:
    row = _stored_row(contact)
    values = row.as_dict()
    return tuple(values[field] for field in BUSINESS_FIELDS)


def row_signature(row: ContactsCsvRow) -> tuple[str, ...]:
    values = row.as_dict()
    return tuple(values[field] for field in BUSINESS_FIELDS)


def _contact_projection(select, Contact):
    return select(Contact.id, *(getattr(Contact, field) for field in BUSINESS_FIELDS))


def _projected_snapshot(rows) -> tuple[tuple[int, tuple[str, ...]], ...]:
    return tuple(
        (int(contact_id), tuple(str(value or "") for value in values))
        for contact_id, *values in rows
    )


def _load_maps(db, *, subject_id: int, identities: set[str], lock: bool = False):
    if not identities:
        return []
    from sqlalchemy import select

    from fakturek.models import ImportMap

    result = []
    values = sorted(identities)
    for start in range(0, len(values), 500):
        statement = (
            select(ImportMap)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(ImportMap.entity_type == "contact")
            .where(ImportMap.external_id.in_(values[start : start + 500]))
            .order_by(ImportMap.external_id)
        )
        if lock:
            statement = statement.with_for_update()
        result.extend(db.scalars(statement).all())
    return result


def build_contacts_csv_import_plan(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
) -> ContactsCsvPlan:
    """Build a complete immutable plan from one destination snapshot."""

    from sqlalchemy import select

    from fakturek.models import Contact, Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if str(getattr(run, "source", "") or "").strip().lower() != SOURCE:
        raise ValueError("Import run is not a contacts CSV v1 run")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")

    rows = tuple(
        parse_contacts_csv_bytes(
            _load_payload(
                run,
                import_storage_root=import_storage_root,
                max_upload_bytes=max_upload_bytes,
            ),
            max_upload_bytes=min(int(max_upload_bytes), MAX_UPLOAD_BYTES),
        )
    )
    contact_snapshot = _projected_snapshot(
        db.execute(
            _contact_projection(select, Contact)
            .where(Contact.subject_id == int(subject_id))
            .order_by(Contact.id)
        ).all()
    )
    by_id = dict(contact_snapshot)
    by_signature: dict[tuple[str, ...], int] = {}
    for contact_id, signature in contact_snapshot:
        by_signature.setdefault(signature, contact_id)

    identities = {row_identity(row) for row in rows}
    maps = _load_maps(db, subject_id=int(subject_id), identities=identities)
    mapped = {str(item.external_id): int(item.internal_id) for item in maps}
    seen: set[str] = set()
    actions: list[ContactsCsvAction] = []
    for row in rows:
        identity = row_identity(row)
        if identity in seen:
            actions.append(ContactsCsvAction(identity=identity, row=row, action="duplicate"))
            continue
        seen.add(identity)
        map_known = identity in mapped
        mapped_signature = by_id.get(mapped[identity]) if map_known else None
        if mapped_signature is not None:
            action = (
                "reuse"
                if mapped_signature == row_signature(row)
                else "mapped_preserved"
            )
            actions.append(
                ContactsCsvAction(
                    identity=identity,
                    row=row,
                    action=action,
                    existing_id=int(mapped[identity]),
                    map_known=True,
                )
            )
            continue
        exact_id = by_signature.get(row_signature(row))
        actions.append(
            ContactsCsvAction(
                identity=identity,
                row=row,
                action="reuse" if exact_id is not None else "create",
                existing_id=int(exact_id) if exact_id is not None else None,
                map_known=map_known,
                map_orphan=map_known,
            )
        )

    return ContactsCsvPlan(
        run_id=int(getattr(run, "id", 0) or 0),
        subject_id=int(subject_id),
        source=SOURCE,
        file_sha256=str(getattr(run, "file_sha256", "") or "").strip().lower(),
        file_size_bytes=int(getattr(run, "file_size_bytes", 0) or 0),
        rows=rows,
        actions=tuple(actions),
        contact_snapshot=contact_snapshot,
        map_snapshot=tuple(sorted(mapped.items())),
    )


def preview_contacts_csv_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ContactsCsvPlan | None = None,
) -> dict[str, Any]:
    plan = plan or build_contacts_csv_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )
    counts = {
        "parsed": len(plan.actions),
        "will_create": 0,
        "will_reuse": 0,
        "will_preserve_mapped": 0,
        "duplicate_rows": 0,
        "will_repair_maps": 0,
    }
    for action in plan.actions:
        if action.action == "duplicate":
            counts["duplicate_rows"] += 1
        elif action.action == "mapped_preserved":
            counts["will_preserve_mapped"] += 1
        else:
            counts[f"will_{action.action}"] += 1
            counts["will_repair_maps"] += int(action.map_orphan)
    return {
        "contacts_csv": True,
        "source": SOURCE,
        "ready_note": (
            "CSV prošlo úplnou kontrolou. Existující kontakt se znovu použije jen při "
            "přesné shodě a nikdy se nepřepíše."
        ),
        "contacts": counts,
        "sample_rows": [row.as_dict() for row in plan.rows[:5]],
    }


def _mapped_contact(db, *, subject_id: int, identity: str):
    from sqlalchemy import select

    from fakturek.models import Contact, ImportMap

    mapped_id = db.scalar(
        select(ImportMap.internal_id)
        .where(ImportMap.subject_id == int(subject_id))
        .where(ImportMap.source == SOURCE)
        .where(ImportMap.entity_type == "contact")
        .where(ImportMap.external_id == identity)
        .limit(1)
    )
    if mapped_id is None:
        return None
    return db.scalar(
        select(Contact)
        .where(Contact.subject_id == int(subject_id))
        .where(Contact.id == int(mapped_id))
        .limit(1)
    )


def _new_contact(*, subject_id: int, row: ContactsCsvRow):
    from fakturek.models import Contact

    values = row.as_dict()
    return Contact(subject_id=int(subject_id), **values)


def _bind_or_repair_map(
    db,
    *,
    subject_id: int,
    action: ContactsCsvAction,
    contact,
):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

    if _stored_signature(contact) != row_signature(action.row) and not action.map_known:
        raise ValueError("Contact changed during processing")
    try:
        with db.begin_nested():
            if action.map_orphan:
                mapping = db.scalar(
                    select(ImportMap)
                    .where(ImportMap.subject_id == int(subject_id))
                    .where(ImportMap.source == SOURCE)
                    .where(ImportMap.entity_type == "contact")
                    .where(ImportMap.external_id == action.identity)
                    .with_for_update()
                    .limit(1)
                )
                if mapping is None:
                    raise ValueError("Contacts CSV mapping changed during processing")
                mapping.internal_id = int(contact.id)
                db.add(mapping)
            elif not action.map_known:
                db.add(
                    ImportMap(
                        subject_id=int(subject_id),
                        source=SOURCE,
                        entity_type="contact",
                        external_id=action.identity,
                        internal_id=int(contact.id),
                    )
                )
            db.flush()
        return contact, False
    except IntegrityError as collision:
        winner = _mapped_contact(db, subject_id=subject_id, identity=action.identity)
        if winner is None:
            raise collision
        return winner, True


def _create_and_claim(db, *, subject_id: int, action: ContactsCsvAction):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

    try:
        with db.begin_nested():
            contact = _new_contact(subject_id=subject_id, row=action.row)
            db.add(contact)
            db.flush()
            if action.map_orphan:
                mapping = db.scalar(
                    select(ImportMap)
                    .where(ImportMap.subject_id == int(subject_id))
                    .where(ImportMap.source == SOURCE)
                    .where(ImportMap.entity_type == "contact")
                    .where(ImportMap.external_id == action.identity)
                    .with_for_update()
                    .limit(1)
                )
                if mapping is None:
                    raise ValueError("Contacts CSV mapping changed during processing")
                mapping.internal_id = int(contact.id)
                db.add(mapping)
            else:
                db.add(
                    ImportMap(
                        subject_id=int(subject_id),
                        source=SOURCE,
                        entity_type="contact",
                        external_id=action.identity,
                        internal_id=int(contact.id),
                    )
                )
            db.flush()
        return contact, False
    except IntegrityError as collision:
        winner = _mapped_contact(db, subject_id=subject_id, identity=action.identity)
        if winner is None:
            raise collision
        return winner, True


def _apply_action(db, *, subject_id: int, action: ContactsCsvAction, contacts_by_id):
    if action.action in {"reuse", "mapped_preserved"}:
        contact = contacts_by_id.get(int(action.existing_id or 0))
        if contact is None:
            raise ValueError("Contact changed during processing")
        return _bind_or_repair_map(
            db,
            subject_id=subject_id,
            action=action,
            contact=contact,
        )
    return _create_and_claim(db, subject_id=subject_id, action=action)


def _chunks(values: list[ContactsCsvAction], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _insert_bind_chunk(db, *, subject_id: int, actions: list[ContactsCsvAction]) -> None:
    from sqlalchemy import insert

    from fakturek.models import ImportMap

    with db.begin_nested():
        db.execute(
            insert(ImportMap),
            [
                {
                    "subject_id": int(subject_id),
                    "source": SOURCE,
                    "entity_type": "contact",
                    "external_id": action.identity,
                    "internal_id": int(action.existing_id or 0),
                }
                for action in actions
            ],
        )


def _insert_create_chunk(db, *, subject_id: int, actions: list[ContactsCsvAction]) -> int:
    from sqlalchemy import insert

    from fakturek.models import Contact, ImportMap

    if not bool(getattr(db.get_bind().dialect, "insert_executemany_returning", False)):
        with db.begin_nested():
            contacts = [
                _new_contact(subject_id=int(subject_id), row=action.row)
                for action in actions
            ]
            db.add_all(contacts)
            db.flush()
            db.add_all(
                [
                    ImportMap(
                        subject_id=int(subject_id),
                        source=SOURCE,
                        entity_type="contact",
                        external_id=action.identity,
                        internal_id=int(contact.id),
                    )
                    for action, contact in zip(actions, contacts, strict=True)
                ]
            )
            db.flush()
        return 0

    with db.begin_nested():
        returned = db.execute(
            insert(Contact).returning(
                Contact.id, *(getattr(Contact, field) for field in BUSINESS_FIELDS)
            ),
            [
                {"subject_id": int(subject_id), **action.row.as_dict()}
                for action in actions
            ],
        ).all()
        ids_by_signature = {
            tuple(str(value or "") for value in values): int(contact_id)
            for contact_id, *values in returned
        }
        map_values = []
        for action in actions:
            contact_id = ids_by_signature.get(row_signature(action.row))
            if contact_id is None:
                raise RuntimeError("contacts CSV batch insert did not return a created row")
            map_values.append(
                {
                    "subject_id": int(subject_id),
                    "source": SOURCE,
                    "entity_type": "contact",
                    "external_id": action.identity,
                    "internal_id": contact_id,
                }
            )
        db.execute(insert(ImportMap), map_values)
    return 0


def _insert_orphan_create_chunk(
    db,
    *,
    subject_id: int,
    actions: list[ContactsCsvAction],
    mappings: dict[str, object],
) -> None:
    with db.begin_nested():
        contacts = [
            _new_contact(subject_id=int(subject_id), row=action.row) for action in actions
        ]
        db.add_all(contacts)
        db.flush()
        for action, contact in zip(actions, contacts, strict=True):
            mapping = mappings.get(action.identity)
            if mapping is None:
                raise ValueError("Contacts CSV mapping changed during processing")
            mapping.internal_id = int(contact.id)
            db.add(mapping)
        db.flush()


def process_contacts_csv_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: ContactsCsvPlan | None = None,
) -> dict[str, Any]:
    """Apply one revalidated plan atomically without overwriting contacts."""

    from sqlalchemy import select, update

    from fakturek.models import Contact, ImportRun, Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    plan = plan or build_contacts_csv_import_plan(
        db,
        run=run,
        subject_id=subject_id,
        import_storage_root=import_storage_root,
        max_upload_bytes=max_upload_bytes,
    )

    if (
        plan.run_id != int(getattr(run, "id", 0) or 0)
        or plan.subject_id != int(subject_id)
        or plan.source != SOURCE
        or str(getattr(run, "source", "") or "").strip().lower() != SOURCE
        or plan.file_sha256 != str(getattr(run, "file_sha256", "") or "").strip().lower()
        or plan.file_size_bytes != int(getattr(run, "file_size_bytes", 0) or 0)
    ):
        raise ValueError("Contacts CSV plan does not match the import run")
    claim = db.execute(
        update(ImportRun)
        .where(ImportRun.id == int(getattr(run, "id", 0) or 0))
        .where(ImportRun.subject_id == int(subject_id))
        .values(status=ImportRun.status)
    )
    if int(getattr(claim, "rowcount", 0) or 0) != 1:
        raise ValueError("Import run does not belong to the current subject")
    if (
        db.scalar(
            select(Subject)
            .where(Subject.id == int(subject_id))
            .with_for_update()
            .limit(1)
        )
        is None
    ):
        raise ValueError("Subject does not exist")
    current_rows = tuple(
        parse_contacts_csv_bytes(
            _load_payload(
                run,
                import_storage_root=import_storage_root,
                max_upload_bytes=max_upload_bytes,
            ),
            max_upload_bytes=min(int(max_upload_bytes), MAX_UPLOAD_BYTES),
        )
    )
    if current_rows != plan.rows:
        raise ValueError("Contacts CSV changed since preview; import was not applied")
    current_snapshot = _projected_snapshot(
        db.execute(
            _contact_projection(select, Contact)
            .where(Contact.subject_id == int(subject_id))
            .order_by(Contact.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if current_snapshot != plan.contact_snapshot:
        raise ValueError("Contact destination changed since preview; import was not applied")
    identities = {action.identity for action in plan.actions}
    maps = _load_maps(db, subject_id=int(subject_id), identities=identities, lock=True)
    current_maps = tuple(sorted((str(item.external_id), int(item.internal_id)) for item in maps))
    if current_maps != plan.map_snapshot:
        raise ValueError("Contacts CSV mappings changed since preview; import was not applied")
    mappings_by_identity = {str(item.external_id): item for item in maps}

    summary: dict[str, Any] = {
        "phase": "contacts_csv_v1",
        "source": SOURCE,
        "contacts": {
            "parsed": len(plan.actions),
            "created": 0,
            "reused": 0,
            "mapped_preserved": 0,
            "duplicate_rows": sum(action.action == "duplicate" for action in plan.actions),
            "repaired_maps": 0,
        },
        "note": "Existing contacts were never overwritten.",
    }
    contact_ids = {contact_id for contact_id, _signature in current_snapshot}
    preserved = [action for action in plan.actions if action.action == "mapped_preserved"]
    if any(int(action.existing_id or 0) not in contact_ids for action in preserved):
        raise ValueError("Contact changed during processing")
    summary["contacts"]["mapped_preserved"] = len(preserved)

    reuse = [action for action in plan.actions if action.action == "reuse"]
    if any(int(action.existing_id or 0) not in contact_ids for action in reuse):
        raise ValueError("Contact changed during processing")
    ordinary_binds = [action for action in reuse if not action.map_known]
    from sqlalchemy.exc import IntegrityError

    for chunk in _chunks(ordinary_binds, _DB_CHUNK_SIZE):
        try:
            _insert_bind_chunk(db, subject_id=int(subject_id), actions=chunk)
        except IntegrityError as collision:
            for action in chunk:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.subject_id == int(subject_id))
                    .where(Contact.id == int(action.existing_id or 0))
                    .limit(1)
                )
                if contact is None:
                    raise ValueError("Contact changed during processing") from collision
                _bind_or_repair_map(
                    db,
                    subject_id=int(subject_id),
                    action=action,
                    contact=contact,
                )
    orphan_binds = [action for action in reuse if action.map_orphan]
    for chunk in _chunks(orphan_binds, _DB_CHUNK_SIZE):
        with db.begin_nested():
            for action in chunk:
                mapping = mappings_by_identity.get(action.identity)
                if mapping is None:
                    raise ValueError("Contacts CSV mapping changed during processing")
                mapping.internal_id = int(action.existing_id or 0)
                db.add(mapping)
            db.flush()
    summary["contacts"]["reused"] = len(reuse)
    summary["contacts"]["repaired_maps"] += len(orphan_binds)

    creates = [action for action in plan.actions if action.action == "create"]
    ordinary_creates = [action for action in creates if not action.map_orphan]
    for chunk in _chunks(ordinary_creates, _DB_CHUNK_SIZE):
        try:
            lost = _insert_create_chunk(db, subject_id=int(subject_id), actions=chunk)
            summary["contacts"]["created"] += len(chunk) - lost
            summary["contacts"]["reused"] += lost
        except IntegrityError:
            for action in chunk:
                _contact, lost = _create_and_claim(
                    db, subject_id=int(subject_id), action=action
                )
                summary["contacts"]["reused" if lost else "created"] += 1
    orphan_creates = [action for action in creates if action.map_orphan]
    for chunk in _chunks(orphan_creates, _DB_CHUNK_SIZE):
        _insert_orphan_create_chunk(
            db,
            subject_id=int(subject_id),
            actions=chunk,
            mappings=mappings_by_identity,
        )
        summary["contacts"]["created"] += len(chunk)
        summary["contacts"]["repaired_maps"] += len(chunk)
    return summary

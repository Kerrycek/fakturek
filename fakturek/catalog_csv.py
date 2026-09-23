"""Strict, portable catalog CSV v1 import and export.

The format is intentionally small and has no database identifiers.  A digest
of the canonical complete row is the import identity, scoped to the target
subject through :class:`ImportMap`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fakturek.security import csv_safe_cell

SOURCE = "fakturek_catalog_csv_v1"
VERSION = "1"
HEADER = (
    "fakturek_catalog_csv_version",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "vat_rate",
    "currency",
)
MAX_ROWS = 100_000
MAX_FIELD_CHARS = 1_048_576
_TWOPLACES = Decimal("0.01")
_MAX_QUANTITY = Decimal("99999999.99")
_MAX_CENTS = 2_147_483_647
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:[.,]\d{1,2})?\Z")
_DB_CHUNK_SIZE = 500
_CREATE_CHUNK_SIZE = 500

# The stdlib parser otherwise permits arbitrarily large individual cells.  The
# value is deliberately far above the v1 field limits so it cannot reject a
# valid row, but caps work before an attacker can make csv allocate freely.
csv.field_size_limit(MAX_FIELD_CHARS)


@dataclass(frozen=True)
class CatalogCsvAction:
    identity: str
    row: dict[str, str]
    action: str
    existing_id: int | None = None
    map_known: bool = False


@dataclass(frozen=True)
class CatalogCsvPlan:
    rows: list[dict[str, str]]
    actions: list[CatalogCsvAction]


def _normalise_text(value: object, *, field: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if "\x00" in value:
        raise ValueError(f"{field} contains an invalid character")
    result = " ".join(value.split()).strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def _decimal(value: object, *, field: str, minimum: Decimal, maximum: Decimal) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal")
    text = value.strip()
    if not _DECIMAL_RE.fullmatch(text):
        raise ValueError(f"{field} must be a decimal")
    if "," in text:
        text = text.replace(",", ".")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result < minimum or result > maximum:
        raise ValueError(f"{field} is out of range")
    if result.as_tuple().exponent < -2:
        raise ValueError(f"{field} has too many decimal places")
    if result.is_zero():
        result = Decimal("0")
    return result.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _formula_export(value: str) -> str:
    # csv_safe_cell protects a dangerous leading character.  Doubling a
    # genuine apostrophe makes its use as an escape marker reversible.
    return csv_safe_cell("'" + value if value.startswith("'") else value)


def _formula_import(value: str) -> str:
    if value.startswith("''"):
        return value[1:]
    if value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES):
        return value[1:]
    return value


def normalise_row(raw: dict[str, object], *, decode_formula: bool = True) -> dict[str, str]:
    if set(raw) != set(HEADER):
        raise ValueError("catalog CSV row has an unsupported schema")
    if str(raw[HEADER[0]]) != VERSION:
        raise ValueError("catalog CSV version is unsupported")
    description_raw = str(raw["description"])
    unit_raw = str(raw["unit"])
    if decode_formula:
        description_raw = _formula_import(description_raw)
        unit_raw = _formula_import(unit_raw)
    description = _normalise_text(description_raw, field="description", maximum=255, required=True)
    unit = _normalise_text(unit_raw, field="unit", maximum=32)
    quantity = _decimal(
        str(raw["quantity"]), field="quantity", minimum=Decimal("0.01"), maximum=_MAX_QUANTITY
    )
    price = _decimal(
        str(raw["unit_price"]),
        field="unit_price",
        minimum=Decimal("0"),
        maximum=Decimal(_MAX_CENTS) / Decimal(100),
    )
    vat_rate = _decimal(
        str(raw["vat_rate"]), field="vat_rate", minimum=Decimal("0"), maximum=Decimal("100")
    )
    currency = str(raw["currency"]).strip()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be an uppercase three-letter code")
    return {
        HEADER[0]: VERSION,
        "description": description,
        "quantity": format(quantity, "f"),
        "unit": unit,
        "unit_price": format(price, "f"),
        "vat_rate": format(vat_rate, "f"),
        "currency": currency,
    }


def row_identity(row: dict[str, str]) -> str:
    """Return the tenant-independent digest of the canonical business row."""

    canonical = json.dumps(
        {
            "description": row["description"].casefold(),
            "quantity": format(Decimal(row["quantity"]), "f"),
            "unit": row["unit"],
            "unit_price_cents": int(Decimal(row["unit_price"]) * 100),
            "vat_rate": format(Decimal(row["vat_rate"]), "f"),
            "currency": row["currency"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def row_signature(row: dict[str, str]) -> tuple[object, ...]:
    return (
        row["description"].casefold(),
        Decimal(row["quantity"]),
        row["unit"],
        int((Decimal(row["unit_price"]) * 100).to_integral_exact()),
        Decimal(row["vat_rate"]),
        row["currency"],
    )


def _export_row(item: object) -> dict[str, str]:
    cents = int(getattr(item, "unit_price_cents", 0) or 0)
    return normalise_row(
        {
            HEADER[0]: VERSION,
            "description": str(getattr(item, "description", "") or ""),
            "quantity": format(Decimal(str(getattr(item, "quantity", "0"))), "f"),
            "unit": str(getattr(item, "unit", "") or ""),
            "unit_price": format(Decimal(cents) / Decimal(100), "f"),
            "vat_rate": format(Decimal(str(getattr(item, "vat_rate", "0"))), "f"),
            "currency": str(getattr(item, "currency", "") or ""),
        },
        decode_formula=False,
    )


def build_catalog_csv_bytes(*, catalog_items: list[object], max_rows: int = MAX_ROWS) -> bytes:
    if len(catalog_items) > max_rows:
        raise ValueError(f"Catalog CSV supports at most {max_rows} rows")
    rows = sorted((_export_row(item) for item in catalog_items), key=row_identity)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(HEADER),
        delimiter=";",
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        safe["description"] = _formula_export(row["description"])
        safe["unit"] = _formula_export(row["unit"])
        writer.writerow(safe)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def parse_catalog_csv_bytes(data: bytes, *, max_rows: int = MAX_ROWS) -> list[dict[str, str]]:
    if not data:
        raise ValueError("catalog CSV is empty")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("catalog CSV is not UTF-8") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=";", strict=True)
        header = next(reader, None)
        if header != list(HEADER):
            raise ValueError("catalog CSV header must exactly match v1")
        rows: list[dict[str, str]] = []
        trailing_blank = False
        for line_no, fields in enumerate(reader, start=2):
            if not fields or all(not value.strip() for value in fields):
                trailing_blank = True
                continue
            if trailing_blank:
                raise ValueError(
                    f"catalog CSV has a non-blank row after trailing blank row {line_no}"
                )
            if len(fields) != len(HEADER):
                raise ValueError(f"catalog CSV has an invalid column count at row {line_no}")
            rows.append(normalise_row({field: fields[index] for index, field in enumerate(HEADER)}))
            if len(rows) > max_rows:
                raise ValueError(f"catalog CSV supports at most {max_rows} rows")
    except csv.Error as exc:
        raise ValueError("catalog CSV is malformed") from exc
    return rows


def _load_payload(run: object, *, import_storage_root: Path, max_upload_bytes: int) -> bytes:
    relative = str(getattr(run, "file_path", "") or "").strip()
    root = import_storage_root.resolve()
    target = (root / relative).resolve()
    if not relative or root not in target.parents or not target.is_file():
        raise ValueError("Import file is unavailable")
    data = target.read_bytes()
    if len(data) > max_upload_bytes:
        raise ValueError("catalog CSV is too large")
    expected = str(getattr(run, "file_sha256", "") or "").strip().lower()
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("stored catalog CSV checksum does not match")
    return data


def _mapped_ids(db, *, subject_id: int, identities: set[str]) -> dict[str, int]:
    if not identities:
        return {}
    from sqlalchemy import select

    from fakturek.models import ImportMap

    result: dict[str, int] = {}
    values = sorted(identities)
    for start in range(0, len(values), 500):
        rows = db.execute(
            select(ImportMap.external_id, ImportMap.internal_id)
            .where(ImportMap.subject_id == int(subject_id))
            .where(ImportMap.source == SOURCE)
            .where(ImportMap.entity_type == "catalog_item")
            .where(ImportMap.external_id.in_(values[start : start + 500]))
        ).all()
        result.update({str(external_id): int(internal_id) for external_id, internal_id in rows})
    return result


def build_catalog_csv_import_plan(
    db, *, run: object, subject_id: int, import_storage_root: Path, max_upload_bytes: int
) -> CatalogCsvPlan:
    from sqlalchemy import select

    from fakturek.models import InvoiceCatalogItem, Subject

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if db.scalar(select(Subject.id).where(Subject.id == int(subject_id)).limit(1)) is None:
        raise ValueError("Subject does not exist")
    rows = parse_catalog_csv_bytes(
        _load_payload(
            run, import_storage_root=import_storage_root, max_upload_bytes=max_upload_bytes
        )
    )
    snapshot = list(
        db.scalars(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .order_by(InvoiceCatalogItem.id)
        ).all()
    )
    by_id = {int(item.id): item for item in snapshot}
    by_signature: dict[tuple[object, ...], object] = {}
    for item in snapshot:
        signature = (
            " ".join(str(item.description or "").split()).strip().casefold(),
            Decimal(str(item.quantity)),
            " ".join(str(item.unit or "").split()).strip(),
            int(item.unit_price_cents),
            Decimal(str(item.vat_rate)),
            str(item.currency or "").strip(),
        )
        by_signature.setdefault(signature, item)
    mapped = _mapped_ids(
        db, subject_id=int(subject_id), identities={row_identity(row) for row in rows}
    )
    seen: set[str] = set()
    actions: list[CatalogCsvAction] = []
    for row in rows:
        identity = row_identity(row)
        if identity in seen:
            actions.append(CatalogCsvAction(identity=identity, row=row, action="duplicate"))
            continue
        seen.add(identity)
        map_known = identity in mapped
        existing = by_id.get(mapped[identity]) if map_known else None
        if map_known and existing is None:
            raise ValueError("catalog CSV mapping points to an unavailable record")
        if existing is None:
            existing = by_signature.get(row_signature(row))
        actions.append(
            CatalogCsvAction(
                identity=identity,
                row=row,
                action="reuse" if existing is not None else "create",
                existing_id=int(existing.id) if existing is not None else None,
                map_known=map_known,
            )
        )
    return CatalogCsvPlan(rows=rows, actions=actions)


def preview_catalog_csv_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: CatalogCsvPlan | None = None,
) -> dict[str, Any]:
    if plan is None:
        plan = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=subject_id,
            import_storage_root=import_storage_root,
            max_upload_bytes=max_upload_bytes,
        )
    counts = {"parsed": len(plan.actions), "will_create": 0, "will_reuse": 0, "duplicate_rows": 0}
    for action in plan.actions:
        if action.action == "duplicate":
            counts["duplicate_rows"] += 1
        else:
            counts[f"will_{action.action}"] += 1
    return {
        "catalog_csv": True,
        "source": SOURCE,
        "ready_note": "CSV was fully validated. Existing catalog items are never overwritten.",
        "catalog_items": counts,
        "sample_rows": plan.rows[:5],
    }


def _mapped_entity(db, *, subject_id: int, identity: str):
    from sqlalchemy import select

    from fakturek.models import ImportMap, InvoiceCatalogItem

    mapped_id = db.scalar(
        select(ImportMap.internal_id)
        .where(ImportMap.subject_id == int(subject_id))
        .where(ImportMap.source == SOURCE)
        .where(ImportMap.entity_type == "catalog_item")
        .where(ImportMap.external_id == identity)
        .limit(1)
    )
    if mapped_id is None:
        return None
    item = db.scalar(
        select(InvoiceCatalogItem)
        .where(InvoiceCatalogItem.subject_id == int(subject_id))
        .where(InvoiceCatalogItem.id == int(mapped_id))
        .limit(1)
    )
    if item is None:
        raise ValueError("catalog CSV mapping points to an unavailable record")
    return item


def _bind_map(db, *, subject_id: int, identity: str, item):
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap

    try:
        with db.begin_nested():
            db.add(
                ImportMap(
                    subject_id=int(subject_id),
                    source=SOURCE,
                    entity_type="catalog_item",
                    external_id=identity,
                    internal_id=int(item.id),
                )
            )
            db.flush()
        return item, False
    except IntegrityError:
        winner = _mapped_entity(db, subject_id=subject_id, identity=identity)
        if winner is None:
            raise
        return winner, True


def _create_with_map(db, *, subject_id: int, identity: str, row: dict[str, str]):
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportMap, InvoiceCatalogItem

    try:
        with db.begin_nested():
            item = InvoiceCatalogItem(
                subject_id=int(subject_id),
                description=row["description"],
                quantity=Decimal(row["quantity"]),
                unit=row["unit"],
                unit_price_cents=int(Decimal(row["unit_price"]) * 100),
                vat_rate=Decimal(row["vat_rate"]),
                currency=row["currency"],
            )
            db.add(item)
            db.flush()
            db.add(
                ImportMap(
                    subject_id=int(subject_id),
                    source=SOURCE,
                    entity_type="catalog_item",
                    external_id=identity,
                    internal_id=int(item.id),
                )
            )
            db.flush()
        return item, False
    except IntegrityError:
        winner = _mapped_entity(db, subject_id=subject_id, identity=identity)
        if winner is None:
            raise
        return winner, True


def _chunks(values: list[CatalogCsvAction], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _preload_items(db, *, subject_id: int, item_ids: set[int]) -> dict[int, object]:
    """Load all planned reused rows in bounded chunks, never per action."""

    if not item_ids:
        return {}
    from sqlalchemy import select

    from fakturek.models import InvoiceCatalogItem

    result: dict[int, object] = {}
    values = sorted(item_ids)
    for start in range(0, len(values), _DB_CHUNK_SIZE):
        rows = db.scalars(
            select(InvoiceCatalogItem)
            .where(InvoiceCatalogItem.subject_id == int(subject_id))
            .where(InvoiceCatalogItem.id.in_(values[start : start + _DB_CHUNK_SIZE]))
        ).all()
        result.update({int(item.id): item for item in rows})
    return result


def _map_values(*, subject_id: int, actions: list[CatalogCsvAction], item_ids: dict[str, int]):
    return [
        {
            "subject_id": int(subject_id),
            "source": SOURCE,
            "entity_type": "catalog_item",
            "external_id": action.identity,
            "internal_id": int(item_ids[action.identity]),
        }
        for action in actions
    ]


def _insert_create_chunk(db, *, subject_id: int, actions: list[CatalogCsvAction]) -> int:
    """Insert a chunk of rows and maps in one savepoint.

    A map collision rolls back every speculative entity in the chunk.  The
    caller then falls back to the per-row savepoint race path, which loads the
    mapping winner rather than leaving an orphan catalog row behind.
    """

    from sqlalchemy import insert

    from fakturek.models import ImportMap, InvoiceCatalogItem

    if not bool(getattr(db.get_bind().dialect, "insert_executemany_returning", False)):
        # Plain MySQL does not support INSERT .. RETURNING.  Preserve the
        # correct race-safe path, accepting per-row work on that dialect.
        lost_races = 0
        for action in actions:
            _item, lost_race = _create_with_map(
                db, subject_id=subject_id, identity=action.identity, row=action.row
            )
            lost_races += int(lost_race)
        return lost_races

    values = [
        {
            "subject_id": int(subject_id),
            "description": action.row["description"],
            "quantity": Decimal(action.row["quantity"]),
            "unit": action.row["unit"],
            "unit_price_cents": int(Decimal(action.row["unit_price"]) * 100),
            "vat_rate": Decimal(action.row["vat_rate"]),
            "currency": action.row["currency"],
        }
        for action in actions
    ]
    with db.begin_nested():
        returned = db.execute(
            insert(InvoiceCatalogItem).returning(
                InvoiceCatalogItem.id,
                InvoiceCatalogItem.description,
                InvoiceCatalogItem.quantity,
                InvoiceCatalogItem.unit,
                InvoiceCatalogItem.unit_price_cents,
                InvoiceCatalogItem.vat_rate,
                InvoiceCatalogItem.currency,
            ),
            values,
        ).all()
        item_ids: dict[tuple[object, ...], int] = {}
        for item_id, description, quantity, unit, cents, vat_rate, currency in returned:
            signature = (
                str(description).casefold(),
                Decimal(str(quantity)),
                str(unit),
                int(cents),
                Decimal(str(vat_rate)),
                str(currency),
            )
            item_ids[signature] = int(item_id)
        action_ids: dict[str, int] = {}
        for action in actions:
            item_id = item_ids.get(row_signature(action.row))
            if item_id is None:
                raise RuntimeError("catalog CSV batch insert did not return a created row")
            action_ids[action.identity] = item_id
        db.execute(
            insert(ImportMap),
            _map_values(subject_id=subject_id, actions=actions, item_ids=action_ids),
        )
    return 0


def _insert_bind_chunk(db, *, subject_id: int, actions: list[CatalogCsvAction]) -> None:
    from sqlalchemy import insert

    from fakturek.models import ImportMap

    item_ids = {action.identity: int(action.existing_id or 0) for action in actions}
    with db.begin_nested():
        db.execute(
            insert(ImportMap),
            _map_values(subject_id=subject_id, actions=actions, item_ids=item_ids),
        )


def process_catalog_csv_import(
    db,
    *,
    run: object,
    subject_id: int,
    import_storage_root: Path,
    max_upload_bytes: int,
    plan: CatalogCsvPlan | None = None,
) -> dict[str, Any]:
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from fakturek.models import ImportRun

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run does not belong to the current subject")
    if plan is None:
        plan = build_catalog_csv_import_plan(
            db,
            run=run,
            subject_id=subject_id,
            import_storage_root=import_storage_root,
            max_upload_bytes=max_upload_bytes,
        )
    # Force a real outer write transaction before nested savepoints. SQLite's
    # legacy transaction mode otherwise treats the first SAVEPOINT as outer.
    claim = db.execute(
        update(ImportRun)
        .where(ImportRun.id == int(getattr(run, "id", 0) or 0))
        .where(ImportRun.subject_id == int(subject_id))
        .values(status=ImportRun.status)
    )
    if int(getattr(claim, "rowcount", 0) or 0) != 1:
        raise ValueError("Import run does not belong to the current subject")
    summary: dict[str, Any] = {
        "phase": "catalog_csv_v1",
        "source": SOURCE,
        "catalog_items": {
            "parsed": len(plan.actions),
            "created": 0,
            "reused": 0,
            "duplicate_rows": 0,
        },
        "note": "Existing catalog items were never overwritten.",
    }
    summary["catalog_items"]["duplicate_rows"] = sum(
        action.action == "duplicate" for action in plan.actions
    )
    unique_actions = [action for action in plan.actions if action.action != "duplicate"]
    reuse_actions = [action for action in unique_actions if action.action == "reuse"]
    existing = _preload_items(
        db,
        subject_id=int(subject_id),
        item_ids={int(action.existing_id or 0) for action in reuse_actions},
    )
    if any(int(action.existing_id or 0) not in existing for action in reuse_actions):
        raise ValueError("Catalog item changed during processing")

    # Existing ImportMap rows were bulk-loaded into the immutable plan and are
    # authoritative.  Only signature matches without a map need an insert.
    bind_actions = [action for action in reuse_actions if not action.map_known]
    for chunk in _chunks(bind_actions, _DB_CHUNK_SIZE):
        try:
            _insert_bind_chunk(db, subject_id=int(subject_id), actions=chunk)
        except IntegrityError:
            for action in chunk:
                _bind_map(
                    db,
                    subject_id=int(subject_id),
                    identity=action.identity,
                    item=existing[int(action.existing_id or 0)],
                )
    summary["catalog_items"]["reused"] = len(reuse_actions)

    create_actions = [action for action in unique_actions if action.action == "create"]
    for chunk in _chunks(create_actions, _CREATE_CHUNK_SIZE):
        try:
            lost_races = _insert_create_chunk(db, subject_id=int(subject_id), actions=chunk)
            summary["catalog_items"]["created"] += len(chunk) - int(lost_races)
            summary["catalog_items"]["reused"] += int(lost_races)
        except IntegrityError:
            for action in chunk:
                _item, lost_race = _create_with_map(
                    db,
                    subject_id=int(subject_id),
                    identity=action.identity,
                    row=action.row,
                )
                summary["catalog_items"]["reused" if lost_race else "created"] += 1
    return summary

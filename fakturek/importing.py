from __future__ import annotations
from fakturek.time_utils import utc_now

"""Import helpers (phase-24).

This module provides small building blocks for future import phases:

- run lifecycle helpers for `import_runs`
- idempotence helpers via `import_map`

It intentionally avoids any framework (FastAPI) imports.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fakturek.models import ImportMap, ImportRun


@dataclass(frozen=True)
class ImportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "created": int(self.created),
                "updated": int(self.updated),
                "skipped": int(self.skipped),
                "errors": int(self.errors),
            }
        )


def mark_run_finished(db: Session, run: ImportRun, *, summary: dict | None = None) -> None:
    run.status = "finished"
    run.finished_at = utc_now()
    if summary is not None:
        run.summary_json = json.dumps(summary)
    db.add(run)


def mark_run_error(db: Session, run: ImportRun, *, error: str) -> None:
    run.status = "error"
    run.finished_at = utc_now()
    payload = {"error": str(error)}
    try:
        if run.summary_json:
            prev = json.loads(str(run.summary_json))
            if isinstance(prev, dict):
                payload = {**prev, **payload}
    except Exception:
        pass
    run.summary_json = json.dumps(payload)
    db.add(run)


def lookup_imported_id(
    db: Session,
    *,
    subject_id: int,
    source: str,
    entity_type: str,
    external_id: str,
) -> int | None:
    """Return internal_id if we have seen this external id before."""

    row = db.scalar(
        select(ImportMap.internal_id)
        .where(ImportMap.subject_id == int(subject_id))
        .where(ImportMap.source == str(source))
        .where(ImportMap.entity_type == str(entity_type))
        .where(ImportMap.external_id == str(external_id))
        .limit(1)
    )
    if row is None:
        return None
    try:
        return int(row)
    except Exception:
        return None


def ensure_import_map(
    db: Session,
    *,
    subject_id: int,
    source: str,
    entity_type: str,
    external_id: str,
    internal_id: int,
) -> None:
    """Insert a mapping row if missing (idempotence).

    This helper is designed to be safe inside a larger import transaction.
    It uses a SAVEPOINT (`Session.begin_nested()`) so a unique-constraint race
    does **not** force the whole import to rollback.

    If the mapping already exists, we keep the first mapping.
    """

    subject_id = int(subject_id)
    internal_id = int(internal_id)
    source = str(source)
    entity_type = str(entity_type)
    external_id = str(external_id)

    if not external_id:
        raise ValueError("external_id is required")

    existing = db.scalar(
        select(ImportMap)
        .where(ImportMap.subject_id == subject_id)
        .where(ImportMap.source == source)
        .where(ImportMap.entity_type == entity_type)
        .where(ImportMap.external_id == external_id)
        .limit(1)
    )
    if existing is not None:
        return

    row = ImportMap(
        subject_id=subject_id,
        source=source,
        entity_type=entity_type,
        external_id=external_id,
        internal_id=internal_id,
    )

    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # Another transaction inserted it concurrently.
        # The surrounding import can continue.
        return

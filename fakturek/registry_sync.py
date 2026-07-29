from __future__ import annotations
from fakturek.time_utils import utc_now

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fakturek.company_lookup import (
    CompanyLookupError,
    CompanyPrefill,
    ares_payload_to_contact_prefill,
    fetch_ares_economic_subject,
    lookup_sk_company_prefill_with_cache,
    normalize_ico,
    normalize_sk_ico,
)
from fakturek.models import Contact
from fakturek.settings import get_settings

SYNC_FIELDS = ("name", "street", "city", "zip", "country", "ico", "dic")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def prefill_hash(prefill: CompanyPrefill) -> str:
    payload = {field: _norm(getattr(prefill, field, "")) for field in SYNC_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def contact_hash(contact: Contact) -> str:
    payload = {field: _norm(getattr(contact, field, "")) for field in SYNC_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _lookup_registry(db: Session, contact: Contact) -> tuple[CompanyPrefill, str]:
    settings = get_settings()
    country = _norm(getattr(contact, "country", "") or "CZ").upper() or "CZ"
    ico = _norm(getattr(contact, "ico", ""))
    if not ico:
        raise CompanyLookupError("Kontakt nemá IČO.")
    if country == "SK":
        company, source, provider = lookup_sk_company_prefill_with_cache(
            db,
            normalize_sk_ico(ico),
            rpo_base_url=settings.sk_rpo_base_url,
            rpo_timeout_seconds=settings.sk_rpo_timeout_seconds,
            orsr_base_url=settings.sk_orsr_base_url,
            orsr_timeout_seconds=settings.sk_orsr_timeout_seconds,
            cache_ttl_days=0,
        )
        return company, f"{provider}:{source}"
    if country != "CZ":
        raise CompanyLookupError("Automatická aktualizace podporuje jen CZ a SK kontakty.")
    payload = fetch_ares_economic_subject(
        normalize_ico(ico),
        base_url=settings.ares_base_url,
        timeout_seconds=settings.ares_timeout_seconds,
    )
    return ares_payload_to_contact_prefill(payload), "ares:live"


def apply_registry_prefill(contact: Contact, prefill: CompanyPrefill, *, source: str, checked_at: datetime | None = None) -> dict[str, Any]:
    now = checked_at or utc_now()
    before = {field: _norm(getattr(contact, field, "")) for field in SYNC_FIELDS}
    after = {field: _norm(getattr(prefill, field, "")) for field in SYNC_FIELDS}
    changes = {field: {"old": before[field], "new": after[field]} for field in SYNC_FIELDS if after[field] and after[field] != before[field]}
    for field, change in changes.items():
        setattr(contact, field, change["new"] or None)
    contact.registry_last_checked_at = now
    contact.registry_last_source = source
    contact.registry_last_error = None
    contact.registry_data_hash = prefill_hash(prefill)
    if changes:
        contact.registry_last_changed_at = now
        contact.registry_update_count = int(getattr(contact, "registry_update_count", 0) or 0) + 1
    return {"changed": bool(changes), "changes": changes, "source": source}


def sync_contact_from_registry(db: Session, contact: Contact, *, force: bool = False) -> dict[str, Any]:
    if not force and not bool(getattr(contact, "registry_auto_update", True)):
        return {"skipped": True, "reason": "disabled"}
    try:
        prefill, source = _lookup_registry(db, contact)
        result = apply_registry_prefill(contact, prefill, source=source)
        db.add(contact)
        return result
    except CompanyLookupError as exc:
        contact.registry_last_checked_at = utc_now()
        contact.registry_last_error = str(exc)
        db.add(contact)
        return {"changed": False, "error": str(exc)}


def sync_due_contacts(db: Session, *, subject_id: int | None = None, max_contacts: int = 100, stale_after_days: int = 7) -> dict[str, Any]:
    cutoff = utc_now() - timedelta(days=max(int(stale_after_days), 1))
    stmt = select(Contact).where(Contact.registry_auto_update.is_(True)).where(Contact.ico.is_not(None)).where(Contact.ico != "")
    if subject_id is not None:
        stmt = stmt.where(Contact.subject_id == int(subject_id))
    stmt = stmt.where((Contact.registry_last_checked_at.is_(None)) | (Contact.registry_last_checked_at < cutoff)).order_by(Contact.registry_last_checked_at.asc(), Contact.id.asc()).limit(max(int(max_contacts), 1))
    contacts = list(db.scalars(stmt).all())
    summary = {"checked": 0, "changed": 0, "errors": 0, "skipped": 0, "contacts": []}
    for contact in contacts:
        result = sync_contact_from_registry(db, contact)
        summary["checked"] += 1
        if result.get("skipped"):
            summary["skipped"] += 1
        if result.get("changed"):
            summary["changed"] += 1
        if result.get("error"):
            summary["errors"] += 1
        summary["contacts"].append({"id": int(contact.id), "name": contact.name, **result})
    return summary

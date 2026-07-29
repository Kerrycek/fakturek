from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from fakturek.models import ApiToken, UserSubject

TOKEN_PREFIX = "ftk_pat_"


def hash_api_token_value(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("token is required")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_token_value() -> str:
    # Token is random and only shown once. A plain SHA-256 hash in storage is
    # acceptable because the input has high entropy.
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32).rstrip('=')}"


def create_api_token(
    db: Session,
    *,
    user_id: int,
    subject_id: int | None = None,
    name: str,
    expires_at: datetime | None = None,
    can_read: bool | None = None,
    can_write: bool | None = None,
    can_issue: bool | None = None,
    can_export: bool | None = None,
    is_sandbox: bool = False,
) -> tuple[ApiToken, str]:
    label = str(name or "").strip() or "API token"
    resolved_subject_id: int | None = None
    resolved_link: UserSubject | None = None

    # Most tests and parts of the app use sessions with ``autoflush=False``.
    # Flush pending ``UserSubject`` rows first so subject-scope resolution sees
    # the current in-session RBAC state before token creation.
    db.flush()

    if subject_id is not None:
        link = db.scalar(
            select(UserSubject)
            .where(UserSubject.user_id == int(user_id))
            .where(UserSubject.subject_id == int(subject_id))
            .limit(1)
        )
        if link is None or not bool(getattr(link, "can_view", False)):
            raise ValueError("User does not have visible access to this subject")
        resolved_subject_id = int(subject_id)
        resolved_link = link
    else:
        subject_ids = [
            int(value)
            for value in db.scalars(
                select(UserSubject.subject_id)
                .where(UserSubject.user_id == int(user_id))
                .where(UserSubject.can_view.is_(True))
                .distinct()
                .order_by(UserSubject.subject_id.asc())
            ).all()
        ]
        if len(subject_ids) == 1:
            resolved_subject_id = int(subject_ids[0])
            resolved_link = db.scalar(
                select(UserSubject)
                .where(UserSubject.user_id == int(user_id))
                .where(UserSubject.subject_id == int(resolved_subject_id))
                .limit(1)
            )
        elif not subject_ids:
            raise ValueError("User does not have any visible subjects")
        else:
            raise ValueError("subject_id is required for users with multiple subjects")

    if resolved_link is None:
        raise ValueError("User does not have access to selected subject")

    token_can_read = bool(getattr(resolved_link, "can_view", False)) if can_read is None else bool(can_read)
    token_can_write = bool(getattr(resolved_link, "can_edit", False)) if can_write is None else bool(can_write)
    token_can_issue = bool(getattr(resolved_link, "can_issue", False)) if can_issue is None else bool(can_issue)
    token_can_export = bool(getattr(resolved_link, "can_export", False)) if can_export is None else bool(can_export)
    if not token_can_read:
        raise ValueError("API token must be allowed to read its subject")
    if token_can_write and not bool(getattr(resolved_link, "can_edit", False)):
        raise ValueError("User cannot grant write API scope for this subject")
    if token_can_issue and not bool(getattr(resolved_link, "can_issue", False)):
        raise ValueError("User cannot grant issue API scope for this subject")
    if token_can_export and not bool(getattr(resolved_link, "can_export", False)):
        raise ValueError("User cannot grant export API scope for this subject")

    for _ in range(0, 20):
        plain = generate_api_token_value()
        token_hash = hash_api_token_value(plain)
        exists = db.scalar(select(ApiToken.id).where(ApiToken.token_hash == token_hash))
        if exists is None:
            row = ApiToken(
                user_id=int(user_id),
                subject_id=resolved_subject_id,
                name=label,
                token_prefix=plain[:24],
                token_hash=token_hash,
                expires_at=expires_at,
                can_read=token_can_read,
                can_write=token_can_write,
                can_issue=token_can_issue,
                can_export=token_can_export,
                is_sandbox=bool(is_sandbox),
            )
            db.add(row)
            db.flush()
            return row, plain

    raise RuntimeError("Failed to allocate unique API token")

#!/usr/bin/env python3
from __future__ import annotations
from fakturek.time_utils import utc_now

import argparse
import sys
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select

from fakturek.api_tokens import create_api_token
from fakturek.db import Base, get_engine, get_sessionmaker
from fakturek.models import Subject, User, UserSubject
from fakturek.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Fakturek API token")
    parser.add_argument("identifier", help="username or e-mail of the user")
    parser.add_argument("--name", default="CLI token", help="human label stored with the token")
    parser.add_argument("--expires-in-days", type=int, default=0, help="optional expiry in days")
    parser.add_argument("--subject-id", type=int, default=0, help="scope the token to a concrete subject ID")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Using database: {settings.database_url}")

    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        identifier = str(args.identifier or "").strip()
        user = db.scalar(
            select(User)
            .where((User.username == identifier) | (User.email == identifier))
            .limit(1)
        )
        if user is None:
            raise SystemExit(f"User not found: {identifier}")

        expires_at = None
        if int(args.expires_in_days or 0) > 0:
            expires_at = utc_now() + timedelta(days=int(args.expires_in_days))

        scoped_subject_id = int(args.subject_id or 0)
        if scoped_subject_id <= 0:
            subject_ids = [
                int(value)
                for value in db.scalars(
                    select(UserSubject.subject_id)
                    .where(UserSubject.user_id == int(user.id))
                    .where(UserSubject.can_view.is_(True))
                    .distinct()
                    .order_by(UserSubject.subject_id.asc())
                ).all()
            ]
            if len(subject_ids) == 1:
                scoped_subject_id = int(subject_ids[0])
            elif not subject_ids:
                raise SystemExit("User does not have any visible subjects to scope the token to.")
            else:
                print("User has access to multiple subjects. Pick one with --subject-id:")
                rows = db.execute(
                    select(Subject.id, Subject.name, Subject.ico)
                    .join(UserSubject, UserSubject.subject_id == Subject.id)
                    .where(UserSubject.user_id == int(user.id))
                    .where(UserSubject.can_view.is_(True))
                    .order_by(Subject.id.asc())
                ).all()
                for subject_id, subject_name, subject_ico in rows:
                    suffix = f" (IČO {subject_ico})" if str(subject_ico or "").strip() else ""
                    print(f"- {subject_id}: {subject_name}{suffix}")
                raise SystemExit(2)

        row, plain = create_api_token(
            db,
            user_id=int(user.id),
            subject_id=int(scoped_subject_id),
            name=str(args.name or "CLI token"),
            expires_at=expires_at,
        )
        db.commit()
        db.refresh(row)

        subject = db.get(Subject, int(scoped_subject_id))

        print("Created API token")
        print(f"user: {user.username} <{user.email}>")
        if subject is not None:
            suffix = f" (IČO {subject.ico})" if str(subject.ico or "").strip() else ""
            print(f"subject: {subject.id} - {subject.name}{suffix}")
        print(f"token_id: {row.id}")
        print(f"name: {row.name}")
        print(f"prefix: {row.token_prefix}")
        if row.expires_at is not None:
            print(f"expires_at: {row.expires_at.isoformat()}Z")
        print("token:")
        print(plain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

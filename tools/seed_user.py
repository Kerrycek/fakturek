#!/usr/bin/env python3
"""Bootstrap an initial owner user and subject into the Fakturek database.

Designed for self-hosted/container deployments where you want an immediately
usable owner account without manually going through ``/setup``.

Behaviour:
- When the database has no users yet, create the configured owner user.
- When the database contains exactly one legacy default demo user, adopt it by
  renaming it to the configured owner account and filling the configured
  subject profile. This keeps existing foreign keys intact when upgrading an
  older deployment that still started on the demo account.
- Otherwise leave existing users untouched.

Config via env:
- FAKTUREK_BOOTSTRAP_USERNAME
- FAKTUREK_BOOTSTRAP_EMAIL
- FAKTUREK_BOOTSTRAP_PASSWORD
- FAKTUREK_BOOTSTRAP_SUBJECT_ID
- FAKTUREK_BOOTSTRAP_SUBJECT_NAME
- FAKTUREK_BOOTSTRAP_SUBJECT_EMAIL
- FAKTUREK_BOOTSTRAP_SUBJECT_PHONE
- FAKTUREK_BOOTSTRAP_SUBJECT_STREET
- FAKTUREK_BOOTSTRAP_SUBJECT_CITY
- FAKTUREK_BOOTSTRAP_SUBJECT_ZIP
- FAKTUREK_BOOTSTRAP_SUBJECT_COUNTRY
- FAKTUREK_BOOTSTRAP_SUBJECT_ICO
- FAKTUREK_BOOTSTRAP_SUBJECT_DIC
- FAKTUREK_BOOTSTRAP_SUBJECT_DEFAULT_CURRENCY
- FAKTUREK_BOOTSTRAP_SUBJECT_IS_VAT_PAYER
- FAKTUREK_BOOTSTRAP_SUBJECT_PUBLIC_USERNAME
- FAKTUREK_BOOTSTRAP_ADOPT_LEGACY_DEMO (default: 1)
- FAKTUREK_BOOTSTRAP_PRINT_PASSWORD (default: 0)
- FAKTUREK_BOOTSTRAP_CREDENTIALS_FILE (optional)

Exit codes:
- 0 success / skipped
- 1 error
"""

from __future__ import annotations
from fakturek.time_utils import utc_now

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

# When this script is executed as a file ("python tools/seed_user.py"),
# Python puts the *script directory* on sys.path, not the project root.
# That makes "import fakturek" fail in single-container deployments.
#
# We add the repo root to sys.path so imports work reliably.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

@dataclass(frozen=True)
class SeedConfig:
    username: str
    email: str
    password: str
    subject_id: int
    subject_name: str
    subject_email: str
    subject_phone: str
    subject_street: str
    subject_city: str
    subject_zip: str
    subject_country: str
    subject_ico: str
    subject_dic: str
    subject_default_currency: str
    subject_is_vat_payer: bool
    subject_public_username: str | None
    adopt_legacy_demo: bool
    print_password: bool
    creds_file: str | None


_LEGACY_DEMO_USERNAME = "demo"
_LEGACY_DEMO_EMAIL = "demo@example.com"


def _getenv(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None:
        return default
    s = v.strip()
    return s if s else default


def _getenv_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_config() -> SeedConfig:
    from fakturek.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

    app_env = (_getenv("APP_ENV", "dev") or "dev").strip().lower()
    username_default = None if app_env == "prod" else "owner"
    email_default = None if app_env == "prod" else "owner@example.com"

    username = (_getenv("FAKTUREK_BOOTSTRAP_USERNAME", username_default) or "").strip()
    email = (_getenv("FAKTUREK_BOOTSTRAP_EMAIL", email_default) or "").strip()
    password = (_getenv("FAKTUREK_BOOTSTRAP_PASSWORD") or "").strip()

    subj_raw = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_ID", "1") or "1").strip()
    try:
        subject_id = int(subj_raw)
    except ValueError:
        subject_id = 1

    if not password:
        # 20 chars URL-safe (~120 bits). Easy to copy/paste.
        password = secrets.token_urlsafe(15)

    subject_name = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_NAME", "Moje firma s.r.o.") or "Moje firma s.r.o.").strip()
    subject_email = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_EMAIL", email) or email).strip()
    subject_phone = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_PHONE", "") or "").strip()
    subject_street = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_STREET", "") or "").strip()
    subject_city = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_CITY", "") or "").strip()
    subject_zip = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_ZIP", "") or "").strip()
    subject_country = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_COUNTRY", "CZ") or "CZ").strip().upper()
    subject_ico = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_ICO", "") or "").strip()
    subject_dic = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_DIC", "") or "").strip()
    subject_default_currency = (
        (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_DEFAULT_CURRENCY", "CZK") or "CZK").strip().upper()
    )
    subject_is_vat_payer = _getenv_bool("FAKTUREK_BOOTSTRAP_SUBJECT_IS_VAT_PAYER", default=False)
    subject_public_username = (_getenv("FAKTUREK_BOOTSTRAP_SUBJECT_PUBLIC_USERNAME", "moje-firma") or "").strip().lower() or None

    adopt_legacy_demo = _getenv_bool("FAKTUREK_BOOTSTRAP_ADOPT_LEGACY_DEMO", default=True)
    print_password = _getenv_bool("FAKTUREK_BOOTSTRAP_PRINT_PASSWORD", default=False)
    creds_file = _getenv("FAKTUREK_BOOTSTRAP_CREDENTIALS_FILE")

    if app_env == "prod":
        if not username:
            raise ValueError("FAKTUREK_BOOTSTRAP_USERNAME is required in prod")
        if not email:
            raise ValueError("FAKTUREK_BOOTSTRAP_EMAIL is required in prod")

    # Basic sanity.
    if len(username) < 3:
        raise ValueError("FAKTUREK_BOOTSTRAP_USERNAME must be at least 3 characters")
    if "@" not in email:
        raise ValueError("FAKTUREK_BOOTSTRAP_EMAIL must look like an email")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            "FAKTUREK_BOOTSTRAP_PASSWORD must contain between "
            f"{MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters"
        )
    if len(subject_country) != 2:
        raise ValueError("FAKTUREK_BOOTSTRAP_SUBJECT_COUNTRY must have 2 characters")
    if len(subject_default_currency) != 3:
        raise ValueError("FAKTUREK_BOOTSTRAP_SUBJECT_DEFAULT_CURRENCY must have 3 characters")

    return SeedConfig(
        username=username,
        email=email,
        password=password,
        subject_id=subject_id,
        subject_name=subject_name,
        subject_email=subject_email,
        subject_phone=subject_phone,
        subject_street=subject_street,
        subject_city=subject_city,
        subject_zip=subject_zip,
        subject_country=subject_country,
        subject_ico=subject_ico,
        subject_dic=subject_dic,
        subject_default_currency=subject_default_currency,
        subject_is_vat_payer=subject_is_vat_payer,
        subject_public_username=subject_public_username,
        adopt_legacy_demo=adopt_legacy_demo,
        print_password=print_password,
        creds_file=creds_file,
    )


def _apply_subject_config(subject, *, cfg: SeedConfig, set_public_username) -> None:
    subject.name = cfg.subject_name
    subject.email = cfg.subject_email
    subject.phone = cfg.subject_phone
    subject.street = cfg.subject_street
    subject.city = cfg.subject_city
    subject.zip = cfg.subject_zip
    subject.country = cfg.subject_country
    subject.ico = cfg.subject_ico
    subject.dic = cfg.subject_dic
    subject.is_vat_payer = bool(cfg.subject_is_vat_payer)
    subject.default_currency = cfg.subject_default_currency

    set_public_username(subject=subject, preferred=cfg.subject_public_username)


def _sync_legacy_profile(db, *, cfg: SeedConfig, IssuerProfile) -> None:
    from sqlalchemy import select

    profile = db.scalar(select(IssuerProfile).order_by(IssuerProfile.id.asc()).limit(1))
    if profile is None:
        profile = IssuerProfile()
        db.add(profile)
    profile.name = cfg.subject_name
    profile.email = cfg.subject_email
    profile.phone = cfg.subject_phone
    profile.street = cfg.subject_street
    profile.city = cfg.subject_city
    profile.zip = cfg.subject_zip
    profile.country = cfg.subject_country
    profile.ico = cfg.subject_ico
    profile.dic = cfg.subject_dic


def _ensure_user_subject_link(db, *, user_id: int, subject_id: int, UserSubject) -> None:
    from sqlalchemy import select

    link = db.scalar(
        select(UserSubject).where(
            UserSubject.user_id == int(user_id),
            UserSubject.subject_id == int(subject_id),
        )
    )
    if link is None:
        link = UserSubject(
            user_id=int(user_id),
            subject_id=int(subject_id),
            role="owner",
            can_view=True,
            can_edit=True,
            can_issue=True,
        )
        db.add(link)
        return

    link.role = "owner"
    link.can_view = True
    link.can_edit = True
    link.can_issue = True
    db.add(link)


def _is_legacy_demo_user(user) -> bool:
    if user is None:
        return False
    username = str(getattr(user, "username", "") or "").strip().lower()
    email = str(getattr(user, "email", "") or "").strip().lower()
    return username == _LEGACY_DEMO_USERNAME and email == _LEGACY_DEMO_EMAIL


def _print_result(label: str, *, cfg: SeedConfig) -> None:
    print(label)
    print(f"  username: {cfg.username}")
    print(f"  email:    {cfg.email}")
    if cfg.print_password:
        print(f"  password: {cfg.password}")
    print(f"  subject:  {cfg.subject_id}")
    print(f"  subject_name: {cfg.subject_name}")


def _write_credentials_file(cfg: SeedConfig) -> None:
    if not cfg.creds_file:
        return
    try:
        os.makedirs(os.path.dirname(cfg.creds_file) or ".", exist_ok=True)
        fd = os.open(cfg.creds_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"username={cfg.username}\n")
            f.write(f"email={cfg.email}\n")
            f.write(f"password={cfg.password}\n")
            f.write(f"subject_id={cfg.subject_id}\n")
        print(f"seed_user: wrote credentials to: {cfg.creds_file}")
    except Exception as exc:
        print(f"seed_user: could not write creds file: {exc}", file=sys.stderr)
        # Not fatal.


def main() -> int:
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"seed_user: invalid config: {exc}", file=sys.stderr)
        return 1

    try:
        from sqlalchemy import func, select

        from fakturek.auth import hash_password
        from fakturek.db import get_sessionmaker
        from fakturek.models import IssuerProfile, Subject, User, UserSubject
        from fakturek.public_links import PUBLIC_USERNAME_RE, ensure_subject_public_username
    except Exception as exc:
        print(f"seed_user: import error: {exc}", file=sys.stderr)
        return 1

    SessionLocal = get_sessionmaker()

    with SessionLocal() as db:
        try:
            users_count = int(db.scalar(select(func.count(User.id))) or 0)
        except Exception as exc:
            print(f"seed_user: DB query failed: {exc}", file=sys.stderr)
            return 1

        def _ensure_subject(subject_id: int):
            subject = db.get(Subject, int(subject_id))
            if subject is None:
                subject = Subject(id=int(subject_id))
                db.add(subject)
                db.flush()
            return subject

        def _set_public_username(*, subject, preferred: str | None = None) -> None:
            from sqlalchemy import select

            candidate = (preferred or "").strip().lower()
            if candidate and PUBLIC_USERNAME_RE.match(candidate):
                exists = db.scalar(
                    select(Subject.id)
                    .where(Subject.public_username == candidate)
                    .where(Subject.id != int(subject.id))
                )
                if exists is None:
                    subject.public_username = candidate
                    db.add(subject)
                    return
            ensure_subject_public_username(db, subject=subject)
            db.add(subject)

        if users_count > 0:
            if cfg.adopt_legacy_demo and users_count == 1:
                try:
                    single_user = db.scalar(select(User).order_by(User.id.asc()).limit(1))
                except Exception as exc:
                    print(f"seed_user: failed to inspect sole user: {exc}", file=sys.stderr)
                    return 1

                if _is_legacy_demo_user(single_user):
                    try:
                        single_user.username = cfg.username
                        single_user.email = cfg.email
                        single_user.password_hash = hash_password(cfg.password)
                        single_user.is_active = True
                        single_user.last_login_at = utc_now()
                        db.add(single_user)
                        db.flush()

                        subject = _ensure_subject(cfg.subject_id)
                        _apply_subject_config(subject, cfg=cfg, set_public_username=_set_public_username)
                        db.add(subject)

                        _ensure_user_subject_link(
                            db,
                            user_id=int(single_user.id),
                            subject_id=int(cfg.subject_id),
                            UserSubject=UserSubject,
                        )
                        _sync_legacy_profile(db, cfg=cfg, IssuerProfile=IssuerProfile)
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        print(f"seed_user: failed to adopt legacy demo user: {exc}", file=sys.stderr)
                        return 1

                    _print_result("seed_user: adopted legacy demo user", cfg=cfg)
                    _write_credentials_file(cfg)
                    return 0

            print(f"seed_user: users already exist ({users_count}); skipping")
            return 0

        try:
            user = User(
                username=cfg.username,
                email=cfg.email,
                password_hash=hash_password(cfg.password),
                is_active=True,
                last_login_at=utc_now(),
            )
            db.add(user)
            db.flush()

            subject = _ensure_subject(cfg.subject_id)
            _apply_subject_config(subject, cfg=cfg, set_public_username=_set_public_username)
            db.add(subject)

            _ensure_user_subject_link(
                db,
                user_id=int(user.id),
                subject_id=int(cfg.subject_id),
                UserSubject=UserSubject,
            )
            _sync_legacy_profile(db, cfg=cfg, IssuerProfile=IssuerProfile)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"seed_user: failed to create bootstrap owner: {exc}", file=sys.stderr)
            return 1

    _print_result("seed_user: created bootstrap owner", cfg=cfg)
    _write_credentials_file(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

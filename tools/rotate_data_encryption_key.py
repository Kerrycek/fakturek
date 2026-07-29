#!/usr/bin/env python3
"""Rotate encrypted database secrets without exposing their plaintext values."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fakturek.db import get_sessionmaker
from fakturek.models import SubjectBankAccount
from fakturek.security import ENCRYPTED_PREFIX, decrypt_secret, encrypt_secret


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _rotate(value: str | None, *, old_key: str, new_key: str, purpose: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    plaintext = decrypt_secret(raw, secret_key=old_key, purpose=purpose)
    if plaintext is None:
        kind = "encrypted" if raw.startswith(ENCRYPTED_PREFIX) else "stored"
        raise RuntimeError(f"Unable to decrypt {kind} value for purpose {purpose!r}")
    return encrypt_secret(plaintext, secret_key=new_key, purpose=purpose)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Commit the rotation; default is verification only.")
    args = parser.parse_args()

    old_key = _required_env("OLD_DATA_ENCRYPTION_KEY")
    new_key = _required_env("NEW_DATA_ENCRYPTION_KEY")
    if old_key == new_key:
        raise RuntimeError("Old and new data encryption keys must differ")

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        fio_rows = db.query(SubjectBankAccount).filter(SubjectBankAccount.fio_api_token.is_not(None)).all()
        for row in fio_rows:
            rotated = _rotate(
                row.fio_api_token,
                old_key=old_key,
                new_key=new_key,
                purpose="fio-api-token",
            )
            if args.apply:
                row.fio_api_token = rotated

        if args.apply:
            db.commit()
        else:
            db.rollback()

    mode = "rotated" if args.apply else "verified"
    print(f"{mode}: fio={len(fio_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

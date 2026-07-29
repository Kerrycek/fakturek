#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from fakturek.db import get_sessionmaker
from fakturek.registry_sync import sync_due_contacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize stale contact data from ARES/RPO registries.")
    parser.add_argument("--subject-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--stale-days", type=int, default=7)
    args = parser.parse_args()

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        summary = sync_due_contacts(
            db,
            subject_id=args.subject_id,
            max_contacts=max(1, args.limit),
            stale_after_days=max(1, args.stale_days),
        )
        db.commit()
    print(json.dumps({"status": "ok", **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

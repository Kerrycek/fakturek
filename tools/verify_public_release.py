#!/usr/bin/env python3
"""Fail when the self-hosted release contains private hosted-service surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = (
    "fakturek_private",
    "fakturek/integrations/comgate.py",
    "templates/admin.html",
    "templates/admin_billing.html",
    "templates/admin_comgate.html",
    "templates/checkout/subscription.html",
    "templates/payments/comgate_return.html",
    "templates/auth/signup_order.html",
    "templates/auth/signup_pending.html",
    "alembic/versions/20260423_56_platform_admin_flag.py",
    "alembic/versions/20260425_58_subject_subscriptions_and_invoice_origin.py",
    "alembic/versions/20260530_60_subscription_billing_exempt.py",
    "alembic/versions/20260603_68_subscription_trial_payment_requests.py",
    "alembic/versions/20260629_74_comgate_platform_checkout.py",
    "alembic/versions/20260629_75_customer_comgate_connections.py",
    "alembic/versions/20260629_76_comgate_refunds_reconciliation.py",
    "alembic/versions/20260630_77_admin_roles.py",
    "alembic/versions/20260630_78_instance_settings.py",
    "alembic/versions/20260701_79_pending_paid_signups.py",
    "alembic/versions/20260712_81_stripe_trans_id.py",
)

RUNTIME_ROOTS = ("fakturek", "templates", "alembic", "static")
FORBIDDEN_RUNTIME_PATTERNS = {
    "hosted gateway": re.compile("com" + "gate", re.IGNORECASE),
    "Stripe integration": re.compile(r"\b" + "str" + r"ipe\b", re.IGNORECASE),
    "hosted gateway transaction model": re.compile("PaymentGateway" + "Transaction"),
    "paid-signup model": re.compile("PendingPaid" + "Signup"),
    "subscription model": re.compile("Subject" + "Subscription"),
    "platform administrator field": re.compile("is_platform_" + "admin"),
    "platform administrator role": re.compile("admin_" + "role"),
}

FORBIDDEN_REPOSITORY_PATTERNS = {
    "legacy development repository": re.compile("Kerrycek/" + "142", re.IGNORECASE),
    "legacy marketing project": re.compile("lets" + "ball", re.IGNORECASE),
    "private deployment path": re.compile(
        "/var/" + r"www/(?:142|fakturek(?:\.cz)?)",
        re.IGNORECASE,
    ),
    "private infrastructure address": re.compile(
        r"(?:172\.16\.9\." + "142" + r"|185\.8\.164\." + "28" + r")"
    ),
    "Stripe credential": re.compile(r"\b(?:pk|sk|rk)_(?:live|test)_[A-Za-z0-9]+"),
}


def _run(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _tracked_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [ROOT / item.decode() for item in output.split(b"\0") if item]


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-single-root", action="store_true")
    args = parser.parse_args()
    errors: list[str] = []

    present_private_paths = [relative for relative in FORBIDDEN_PATHS if (ROOT / relative).exists()]
    if present_private_paths:
        errors.append(f"Private hosted paths are present: {present_private_paths}")

    runtime_files: list[Path] = []
    for relative_root in RUNTIME_ROOTS:
        path = ROOT / relative_root
        if path.is_dir():
            runtime_files.extend(
                item
                for item in path.rglob("*")
                if item.is_file() and "vendor" not in item.relative_to(ROOT).parts
            )
    for file_path in runtime_files:
        source = _text(file_path)
        if source is None:
            continue
        relative = file_path.relative_to(ROOT).as_posix()
        for label, pattern in FORBIDDEN_RUNTIME_PATTERNS.items():
            if pattern.search(source):
                errors.append(f"{label} found in runtime file {relative}")

    tracked_files = _tracked_files()
    for file_path in tracked_files:
        source = _text(file_path)
        if source is None:
            continue
        relative = file_path.relative_to(ROOT).as_posix()
        for label, pattern in FORBIDDEN_REPOSITORY_PATTERNS.items():
            if pattern.search(source):
                errors.append(f"{label} found in {relative}")

    forbidden_suffixes = {".sql", ".sqlite", ".sqlite3", ".pem", ".key"}
    forbidden_data_files = [
        path.relative_to(ROOT).as_posix()
        for path in tracked_files
        if path.suffix.lower() in forbidden_suffixes or path.name.startswith(".env.local")
    ]
    if forbidden_data_files:
        errors.append(f"Secret/data files are tracked: {forbidden_data_files}")

    symlinks = [path.relative_to(ROOT).as_posix() for path in tracked_files if path.is_symlink()]
    if symlinks:
        errors.append(f"Release contains symlinks: {symlinks}")

    required_files = ("LICENSE", "README.md", "SECURITY.md", "docs/INSTALLATION.md", ".env.example")
    missing_required = [relative for relative in required_files if not (ROOT / relative).is_file()]
    if missing_required:
        errors.append(f"Required release files are missing: {missing_required}")

    if args.require_single_root:
        count = int(_run("rev-list", "--count", "HEAD"))
        roots = _run("rev-list", "--max-parents=0", "HEAD").splitlines()
        head = _run("rev-parse", "HEAD")
        if count != 1 or roots != [head]:
            errors.append("Publication checkout must contain one root commit and no parent history")
        additional_refs = [
            line
            for line in _run(
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads",
                "refs/tags",
                "refs/remotes",
            ).splitlines()
            if line and line != "refs/heads/main"
        ]
        if additional_refs:
            errors.append(f"Publication checkout contains additional Git refs: {additional_refs}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "tracked_files": len(tracked_files),
                "tree": _run("rev-parse", "HEAD^{tree}"),
                "single_root": bool(args.require_single_root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

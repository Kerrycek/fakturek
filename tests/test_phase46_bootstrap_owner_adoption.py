from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _load_seed_user_module():
    module_name = "phase46_seed_user_db"
    path = Path(__file__).resolve().parents[1] / "tools" / "seed_user.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_seed_user_adopts_legacy_demo_account(monkeypatch, tmp_path):
    db_path = tmp_path / "phase46-bootstrap.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_USERNAME", "owner")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_EMAIL", "owner@example.test")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_PASSWORD", "BootstrapPass123!")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_NAME", "Moje firma s.r.o.")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_PHONE", "+420 123 456 789")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_STREET", "Testovací 1")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_CITY", "Praha")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_ZIP", "110 00")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_COUNTRY", "CZ")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_ICO", "12345678")
    monkeypatch.setenv("FAKTUREK_BOOTSTRAP_SUBJECT_PUBLIC_USERNAME", "moje-firma")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.models import Subject, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(Subject(id=1, name="", email=""))
        db.add(
            User(
                id=1,
                username="demo",
                email="demo@example.com",
                password_hash=hash_password("demo12345", iterations=1_000),
                is_active=True,
            )
        )
        db.add(
            UserSubject(
                id=1,
                user_id=1,
                subject_id=1,
                role="owner",
                can_view=True,
                can_edit=True,
                can_issue=True,
            )
        )
        db.commit()

    seed_user = _load_seed_user_module()
    assert seed_user.main() == 0

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == 1).one()
        subject = db.query(Subject).filter(Subject.id == 1).one()
        link = db.query(UserSubject).filter(UserSubject.user_id == 1, UserSubject.subject_id == 1).one()

        assert user.username == "owner"
        assert user.email == "owner@example.test"
        assert subject.name == "Moje firma s.r.o."
        assert subject.phone == "+420 123 456 789"
        assert subject.street == "Testovací 1"
        assert subject.city == "Praha"
        assert subject.zip == "110 00"
        assert subject.country == "CZ"
        assert subject.ico == "12345678"
        assert subject.public_username == "moje-firma"
        assert link.role == "owner"
        assert link.can_view is True
        assert link.can_edit is True
        assert link.can_issue is True

    _reset_settings_and_db()

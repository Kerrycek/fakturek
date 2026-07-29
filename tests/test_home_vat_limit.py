from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def test_home_shows_current_year_vat_limit_progress(monkeypatch, tmp_path):
    db_path = tmp_path / "home-vat.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    current_year = date.today().year
    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Demo Subject s.r.o.",
                    email="demo@example.test",
                    country="CZ",
                    default_currency="CZK",
                ),
                Subject(
                    id=2,
                    name="Other Subject s.r.o.",
                    email="other@example.test",
                    country="CZ",
                    default_currency="CZK",
                ),
                Contact(
                    id=1,
                    subject_id=1,
                    name="Client a.s.",
                    email="billing@client.example.test",
                    country="CZ",
                ),
                Contact(
                    id=2,
                    subject_id=2,
                    name="Other Client a.s.",
                    email="billing@other-client.example.test",
                    country="CZ",
                ),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0001",
                    status="sent",
                    issue_date=date(current_year, 2, 10),
                    due_date=date(current_year, 2, 20),
                    currency="CZK",
                    total_cents=150_000_000,
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0002",
                    status="draft",
                    issue_date=date(current_year, 2, 12),
                    due_date=date(current_year, 2, 22),
                    currency="CZK",
                    total_cents=99_000_000,
                ),
                Invoice(
                    id=3,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0003",
                    status="sent",
                    issue_date=date(current_year, 2, 15),
                    due_date=date(current_year, 2, 25),
                    currency="EUR",
                    total_cents=45_000_000,
                ),
                Invoice(
                    id=4,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year - 1}-0001",
                    status="paid",
                    issue_date=date(current_year - 1, 12, 30),
                    due_date=date(current_year - 1, 12, 31),
                    currency="CZK",
                    total_cents=20_000_000,
                ),
                Invoice(
                    id=5,
                    subject_id=2,
                    contact_id=2,
                    number=f"{current_year}-9001",
                    status="paid",
                    issue_date=date(current_year, 3, 1),
                    due_date=date(current_year, 3, 8),
                    currency="CZK",
                    total_cents=88_000_000,
                ),
            ]
        )
        db.commit()

    client = TestClient(create_app())
    response = client.get("/")

    assert response.status_code == 200
    assert "Limit DPH" in response.text
    assert "1 500 000,00 CZK" in response.text
    assert "500 000,00 CZK" in response.text
    assert "1 036 500,00 CZK" in response.text
    assert "Naposledy vystavené" in response.text
    assert "Roční výkon" in response.text
    assert f"{current_year}-0003" in response.text
    assert f"{current_year}-0001" in response.text
    assert f"{current_year}-0002" not in response.text
    assert str(current_year - 1) in response.text
    assert "Nezahrnuto cizoměnných faktur" in response.text
    assert "Od 1. 1. 2025" in response.text

    _reset_settings_and_db()


def test_home_hides_tax_limit_cards_for_association(monkeypatch, tmp_path):
    db_path = tmp_path / "home-association-tax.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    current_year = date.today().year
    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Demo Spolek, z.s.",
                    email="spolek@example.test",
                    country="CZ",
                    default_currency="CZK",
                    legal_form="association",
                    tax_regime="flat",
                ),
                Contact(id=1, subject_id=1, name="Darca", email="darce@example.test", country="CZ"),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0001",
                    status="sent",
                    issue_date=date(current_year, 2, 10),
                    due_date=date(current_year, 2, 20),
                    currency="CZK",
                    total_cents=2_500_000_00,
                ),
            ]
        )
        db.commit()

    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert "Limit DPH" not in response.text
    assert "Paušální režim" not in response.text
    assert "Naposledy vystavené" in response.text

    _reset_settings_and_db()

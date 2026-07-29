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


def test_stats_page_supports_year_filters_and_charts(monkeypatch, tmp_path):
    db_path = tmp_path / "stats-years.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    current_year = date.today().year
    previous_year = current_year - 1

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Demo Subject s.r.o.",
                email="demo@example.test",
                country="CZ",
                default_currency="CZK",
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Client a.s.",
                email="billing@client.example.test",
                country="CZ",
            )
        )
        db.add_all(
            [
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0001",
                    status="issued",
                    issue_date=date(current_year, 1, 10),
                    due_date=date(current_year, 1, 24),
                    currency="CZK",
                    total_cents=120_000_00,
                ),
                Invoice(
                    id=2,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0002",
                    status="paid",
                    issue_date=date(current_year, 3, 14),
                    due_date=date(current_year, 3, 28),
                    paid_on=date(current_year, 3, 20),
                    currency="CZK",
                    total_cents=80_000_00,
                ),
                Invoice(
                    id=3,
                    subject_id=1,
                    contact_id=1,
                    number=f"{previous_year}-0001",
                    status="sent",
                    issue_date=date(previous_year, 6, 2),
                    due_date=date(previous_year, 6, 16),
                    currency="CZK",
                    total_cents=55_000_00,
                ),
                Invoice(
                    id=4,
                    subject_id=1,
                    contact_id=1,
                    number=f"{previous_year}-0002",
                    status="draft",
                    issue_date=date(previous_year, 8, 1),
                    due_date=date(previous_year, 8, 15),
                    currency="CZK",
                    total_cents=99_000_00,
                ),
                Invoice(
                    id=5,
                    subject_id=1,
                    contact_id=1,
                    number=f"{current_year}-0003",
                    status="sent",
                    issue_date=date(current_year, 5, 2),
                    due_date=date(current_year, 5, 16),
                    currency="EUR",
                    total_cents=10_000_00,
                ),
            ]
        )
        db.commit()

    client = TestClient(create_app(), base_url="https://app.example.test")

    current_response = client.get(f"/stats?year={current_year}")
    assert current_response.status_code == 200
    assert "Vyber rok" in current_response.text
    assert "Vývoj po letech" in current_response.text
    assert "po měsících" in current_response.text
    assert "Posledních 12 měsíců" in current_response.text
    assert "Zaplaceno" in current_response.text
    assert "200 000,00 CZK" in current_response.text
    assert f"/stats?year={previous_year}" in current_response.text

    previous_response = client.get(f"/stats?year={previous_year}")
    assert previous_response.status_code == 200
    assert "55 000,00 CZK" in previous_response.text
    assert f"Stavy faktur za rok {previous_year}" in previous_response.text

    _reset_settings_and_db()

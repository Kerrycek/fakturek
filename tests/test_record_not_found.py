from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _vary_tokens(response) -> set[str]:
    return {item.strip().lower() for item in response.headers.get("vary", "").split(",")}


@pytest.fixture()
def records_client(monkeypatch, tmp_path):
    db_path = tmp_path / "record-not-found.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(id=1, name="Current subject"),
                Subject(id=2, name="FOREIGN SUBJECT SECRET"),
                Contact(id=10, subject_id=1, name="Current contact"),
                Contact(id=20, subject_id=2, name="FOREIGN CONTACT SECRET"),
                Invoice(
                    id=30,
                    subject_id=2,
                    contact_id=20,
                    number="FOREIGN-INVOICE-SECRET",
                    status="issued",
                    issue_date=date(2026, 9, 15),
                    due_date=date(2026, 9, 29),
                    currency="CZK",
                ),
            ]
        )
        db.commit()

    with TestClient(create_app()) as client:
        yield client

    _reset_settings_and_db()


@pytest.mark.parametrize(
    ("resource", "foreign_id", "back_url"),
    [
        ("contacts", 20, "/contacts"),
        ("invoices", 30, "/invoices"),
    ],
)
def test_detail_html_does_not_distinguish_missing_from_foreign_record(
    records_client: TestClient,
    resource: str,
    foreign_id: int,
    back_url: str,
) -> None:
    headers = {"Accept": "text/html"}
    missing = records_client.get(f"/{resource}/999999", headers=headers)
    foreign = records_client.get(f"/{resource}/{foreign_id}", headers=headers)

    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert foreign.headers["content-type"].startswith("text/html")
    assert missing.text == foreign.text

    for response in (missing, foreign):
        assert "accept" in _vary_tokens(response)
        assert "Záznam není dostupný" in response.text
        assert "Požadovaný záznam se nepodařilo otevřít." in response.text
        assert f'href="{back_url}"' in response.text
        assert "FOREIGN SUBJECT SECRET" not in response.text
        assert "FOREIGN CONTACT SECRET" not in response.text
        assert "FOREIGN-INVOICE-SECRET" not in response.text


@pytest.mark.parametrize(
    ("resource", "foreign_id", "detail"),
    [
        ("contacts", 20, "Contact not found"),
        ("invoices", 30, "Invoice not found"),
    ],
)
def test_detail_explicit_json_accept_preserves_not_found_contract(
    records_client: TestClient,
    resource: str,
    foreign_id: int,
    detail: str,
) -> None:
    headers = {"Accept": "application/json"}
    missing = records_client.get(f"/{resource}/999999", headers=headers)
    foreign = records_client.get(f"/{resource}/{foreign_id}", headers=headers)

    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
    assert foreign.headers["content-type"].startswith("application/json")
    assert missing.json() == {"detail": detail}
    assert foreign.json() == {"detail": detail}
    assert missing.content == foreign.content

    for response in (missing, foreign):
        assert "accept" in _vary_tokens(response)
        assert b"FOREIGN SUBJECT SECRET" not in response.content
        assert b"FOREIGN CONTACT SECRET" not in response.content
        assert b"FOREIGN-INVOICE-SECRET" not in response.content


@pytest.mark.parametrize(
    ("accept", "expected_content_type"),
    [
        ("application/json;q=1, text/html;q=0", "application/json"),
        ("application/json, text/html;q=0.1", "application/json"),
        ("text/html, application/json;q=0.5", "text/html"),
        ("text/html, application/json", "text/html"),
        ("application/json;q=0", "text/html"),
    ],
)
def test_detail_not_found_honors_explicit_accept_quality(
    records_client: TestClient,
    accept: str,
    expected_content_type: str,
) -> None:
    response = records_client.get(
        "/contacts/999999",
        headers={"Accept": accept},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(expected_content_type)
    assert "accept" in _vary_tokens(response)

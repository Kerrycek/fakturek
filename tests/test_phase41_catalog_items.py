from __future__ import annotations

from decimal import Decimal

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


def _setup_sqlite_app(monkeypatch, tmp_path, *, is_vat_payer: bool = True, default_currency: str = "CZK"):
    db_path = tmp_path / "phase41.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, InvoiceSeries, Subject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add(
            Subject(
                id=1,
                name="Test subject",
                email="owner@example.test",
                is_vat_payer=is_vat_payer,
                default_currency=default_currency,
            )
        )
        db.add(
            Contact(
                id=1,
                subject_id=1,
                name="Jiří Chvojka",
                email="jiri@example.test",
                ico="12345678",
                street="Dlouhá 1",
                city="Praha",
                zip="11000",
                country="CZ",
            )
        )
        db.add(
            InvoiceSeries(
                id=1,
                subject_id=1,
                name="default",
                prefix="2026-",
                pad_length=4,
                last_counter=0,
            )
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def test_invoice_editor_shows_catalog_sidebar_and_actions(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    res = client.get("/invoices/new")

    assert res.status_code == 200
    assert "Katalog oblíbených položek" in res.text
    assert 'data-save-catalog-item' in res.text
    assert '/invoices/catalog-items' in res.text
    assert 'Našeptání z katalogu a minulých faktur' in res.text

    _reset_settings_and_db()


def test_catalog_item_can_be_saved_listed_suggested_and_deleted(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    response = client.post(
        "/invoices/catalog-items",
        data={
            "description": "Měsíční retainer",
            "quantity": "1",
            "unit": "hod",
            "unit_price": "1500.00",
            "vat_rate": "21",
            "currency": "CZK",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["item"]["description"] == "Měsíční retainer"
    assert payload["item"]["unit"] == "hod"
    item_id = payload["item"]["id"]

    duplicate = client.post(
        "/invoices/catalog-items",
        data={
            "description": "Měsíční  retainer",
            "quantity": "1",
            "unit": "hod",
            "unit_price": "1500.00",
            "vat_rate": "21",
            "currency": "CZK",
        },
    )
    assert duplicate.status_code == 200
    duplicate_payload = duplicate.json()
    assert duplicate_payload["created"] is False
    assert duplicate_payload["item"]["id"] == item_id

    listing = client.get("/invoices/catalog-items?currency=CZK")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["description"] == "Měsíční retainer"
    assert items[0]["unit"] == "hod"

    suggestions = client.get("/invoices/item-suggestions?q=retainer&currency=CZK")
    assert suggestions.status_code == 200
    suggestion_items = suggestions.json()["suggestions"]
    assert suggestion_items
    assert suggestion_items[0]["description"] == "Měsíční retainer"
    assert suggestion_items[0]["unit"] == "hod"
    assert suggestion_items[0]["source"] == "catalog"

    delete_response = client.post(f"/invoices/catalog-items/{item_id}/delete")
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_id"] == item_id

    listing_after_delete = client.get("/invoices/catalog-items?currency=CZK")
    assert listing_after_delete.status_code == 200
    assert listing_after_delete.json()["items"] == []

    from fakturek.models import InvoiceCatalogItem

    with SessionLocal() as db:
        assert db.query(InvoiceCatalogItem).count() == 0

    _reset_settings_and_db()


def test_invoice_editor_prefills_catalog_for_current_currency(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path, default_currency="EUR")

    from fakturek.models import InvoiceCatalogItem

    with SessionLocal() as db:
        db.add(
            InvoiceCatalogItem(
                id=1,
                subject_id=1,
                description="EU služba",
                quantity=Decimal("1.00"),
                unit_price_cents=12345,
                vat_rate=Decimal("21.00"),
                currency="EUR",
            )
        )
        db.add(
            InvoiceCatalogItem(
                id=2,
                subject_id=1,
                description="CZ služba",
                quantity=Decimal("1.00"),
                unit_price_cents=99900,
                vat_rate=Decimal("21.00"),
                currency="CZK",
            )
        )
        db.commit()

    res = client.get("/invoices/new")

    assert res.status_code == 200
    assert "EU služba" in res.text
    assert "CZ služba" not in res.text
    assert 'data-catalog-currency-label' in res.text
    assert '>EUR<' in res.text

    _reset_settings_and_db()

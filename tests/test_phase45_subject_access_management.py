from __future__ import annotations

from datetime import date
import re

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "phase45.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject, User, UserSubject

    from fakturek.db import get_engine, get_sessionmaker

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Owner Subject s.r.o.",
                    email="owner-subject@example.test",
                    public_username="owner-subject",
                    city="Praha",
                    country="CZ",
                    default_currency="CZK",
                    ico="12345678",
                ),
                Subject(
                    id=2,
                    name="Second Subject s.r.o.",
                    email="owner-second@example.test",
                    public_username="second-subject",
                    city="Brno",
                    country="CZ",
                    default_currency="CZK",
                    ico="87654321",
                ),
                Subject(
                    id=3,
                    name="Manager Subject s.r.o.",
                    email="manager-subject@example.test",
                    public_username="manager-subject",
                    city="Ostrava",
                    country="CZ",
                    default_currency="CZK",
                    ico="10293847",
                ),
                Contact(
                    id=1,
                    subject_id=1,
                    name="Owner Client a.s.",
                    email="billing@owner-client.example.test",
                    city="Praha",
                    country="CZ",
                ),
                User(
                    id=1,
                    username="owner",
                    email="owner@example.test",
                    password_hash=hash_password("secret123", iterations=1_000),
                    is_active=True,
                ),
                User(
                    id=2,
                    username="manager",
                    email="manager@example.test",
                    password_hash=hash_password("secret123", iterations=1_000),
                    is_active=True,
                ),
                UserSubject(
                    id=1,
                    user_id=1,
                    subject_id=1,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                UserSubject(
                    id=2,
                    user_id=1,
                    subject_id=2,
                    role="owner",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                UserSubject(
                    id=3,
                    user_id=2,
                    subject_id=1,
                    role="manager",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                UserSubject(
                    id=4,
                    user_id=1,
                    subject_id=3,
                    role="manager",
                    can_view=True,
                    can_edit=True,
                    can_issue=True,
                ),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0001",
                    status="issued",
                    issue_date=date(2026, 3, 1),
                    due_date=date(2026, 3, 15),
                    currency="CZK",
                    total_cents=12500,
                    buyer_name_cache="Owner Client a.s.",
                ),
            ]
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _login(client: TestClient, identifier: str) -> None:
    response = client.post(
        "/login",
        data={
            "identifier": identifier,
            "password": "secret123",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _switch_subject(client: TestClient, subject_id: int, next_url: str = "/settings#subjects-admin") -> None:
    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    html = settings_page.text
    form_match = re.search(
        rf'<form method="post" action="/subjects/{int(subject_id)}/switch" class="inline-form">(.*?)</form>',
        html,
        re.DOTALL,
    )
    assert form_match is not None
    form_html = form_match.group(1)
    assert f'name="next" value="{next_url}"' in form_html
    token_match = re.search(r'name="st" value="([^"]+)"', form_html)
    switch_token = token_match.group(1) if token_match else ""
    response = client.post(
        f"/subjects/{subject_id}/switch",
        data={"next": next_url, "st": switch_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_owner_can_update_and_delete_subject_access(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client, "owner")

    update = client.post(
        "/settings/subjects/1/users/3/update",
        data={
            "next": "/settings#subjects-admin",
            "role": "user",
            "can_view": "1",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303
    assert update.headers["location"] == "/settings?saved=1#subjects-admin"

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        link = db.query(UserSubject).filter(UserSubject.id == 3).one()
        assert link.role == "user"
        assert link.can_view is True
        assert link.can_edit is False
        assert link.can_issue is False

    delete = client.post(
        "/settings/subjects/1/users/3/delete",
        data={"next": "/settings#subjects-admin"},
        follow_redirects=False,
    )
    assert delete.status_code == 303
    assert delete.headers["location"] == "/settings?saved=1#subjects-admin"

    with SessionLocal() as db:
        assert db.query(UserSubject).filter(UserSubject.id == 3).count() == 0

    _reset_settings_and_db()

def test_owner_can_link_existing_user_to_another_subject(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client, "owner")
    _switch_subject(client, 2)

    response = client.post(
        "/settings/subjects/2/users/link-existing",
        data={
            "next": "/settings#subjects-admin",
            "identifier": "manager@example.test",
            "role": "manager",
            "can_view": "1",
            "can_edit": "1",
            "can_issue": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1#subjects-admin"

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        link = (
            db.query(UserSubject)
            .filter(UserSubject.user_id == 2, UserSubject.subject_id == 2)
            .one()
        )
        assert link.role == "manager"
        assert link.can_edit is True

    _reset_settings_and_db()




def test_last_owner_cannot_be_removed(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client, "owner")
    _switch_subject(client, 2)

    response = client.post(
        "/settings/subjects/2/users/2/delete",
        data={"next": "/settings#subjects-admin"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Posledního ownera" in response.text

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        assert db.query(UserSubject).filter(UserSubject.id == 2).count() == 1

    _reset_settings_and_db()

def test_manager_cannot_edit_owner_access_even_via_route(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client, "manager")

    response = client.post(
        "/settings/subjects/1/users/1/update",
        data={
            "next": "/settings#subjects-admin",
            "role": "manager",
            "can_view": "1",
            "can_edit": "1",
            "can_issue": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        link = db.query(UserSubject).filter(UserSubject.id == 1).one()
        assert link.role == "owner"

    _reset_settings_and_db()

def test_user_without_issue_permission_cannot_open_new_invoice_editor(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        link = db.query(UserSubject).filter(UserSubject.id == 3).one()
        link.can_issue = False
        db.add(link)
        db.commit()

    _login(client, "manager")

    response = client.get("/invoices/new")
    assert response.status_code == 403
    assert "Na tuhle akci nemáš práva" in response.text
    assert "vystavovat doklady za tento subjekt" in response.text
    assert "Prohlížet faktury" in response.text

    response = client.post(
        "/invoices/new",
        data={
            "contact_id": "1",
            "number": "2026-0002",
            "issue_date": "2026-03-01",
            "due_date": "2026-03-15",
            "currency": "CZK",
            "payment_method": "bank",
            "status": "issued",
            "item_description": "Test",
            "item_quantity": "1",
            "item_unit_price": "100",
        },
    )
    assert response.status_code == 403
    assert "Na tuhle akci nemáš práva" in response.text

    json_response = client.get("/invoices/new", headers={"Accept": "application/json"})
    assert json_response.status_code == 403
    assert json_response.json() == {"detail": "Access denied"}

    _reset_settings_and_db()


def test_viewer_cannot_open_invoice_or_contact_editors(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import UserSubject

    with SessionLocal() as db:
        link = db.query(UserSubject).filter(UserSubject.id == 3).one()
        link.role = "viewer"
        link.can_view = True
        link.can_edit = False
        link.can_issue = False
        db.add(link)
        db.commit()

    _login(client, "manager")

    invoice_response = client.get("/invoices/1/edit")
    assert invoice_response.status_code == 403
    assert "Na tuhle akci nemáš práva" in invoice_response.text
    assert "upravovat údaje tohoto subjektu" in invoice_response.text

    contact_response = client.get("/contacts/1/edit")
    assert contact_response.status_code == 403
    assert "Na tuhle akci nemáš práva" in contact_response.text
    assert "Úpravy jsou zamčené" in contact_response.text

    _reset_settings_and_db()

def test_deleting_own_current_non_owner_subject_switches_session(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import User, UserSubject

    with SessionLocal() as db:
        assert db.query(User).filter(User.id == 1).one() is not None

    _login(client, "owner")
    _switch_subject(client, 3)

    response = client.post(
        "/settings/subjects/3/users/4/delete",
        data={"next": "/settings#subjects-admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1#subjects-admin"

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Aktuální subjekt" in settings_page.text
    assert "Owner Subject s.r.o." in settings_page.text

    with SessionLocal() as db:
        assert db.query(UserSubject).filter(UserSubject.id == 4).count() == 0

    _reset_settings_and_db()



def test_user_without_export_permission_cannot_download_export_endpoints(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    with SessionLocal() as db:
        from fakturek.models import UserSubject

        link = db.get(UserSubject, 3)
        assert link is not None
        link.can_export = False
        db.commit()

    _login(client, "manager")

    for method, url in [
        ("get", "/contacts/export.csv"),
        ("get", "/invoices/export.csv"),
        ("get", "/exports/data.zip"),
        ("post", "/exports/invoices"),
        ("get", "/invoices/1/pdf?download=1"),
        ("get", "/invoices/1/isdoc?download=1"),
    ]:
        response = getattr(client, method)(url, follow_redirects=False)
        assert response.status_code == 403, url

    with SessionLocal() as db:
        from fakturek.models import UserSubject

        link = db.get(UserSubject, 3)
        assert link is not None
        link.can_export = True
        db.commit()

    allowed = client.get("/contacts/export.csv", follow_redirects=False)
    assert allowed.status_code == 200

    _reset_settings_and_db()

from __future__ import annotations

from datetime import date
import io
import re

import pytest
from starlette.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy")

import fakturek.db as db_module
from fakturek.auth import hash_password
from fakturek.banking import BankAccountPayload, build_payment_qr_codes
from fakturek.db import Base
from fakturek.pdf import InvoicePDFData, render_invoice_pdf_bytes
from fakturek.public_links import build_public_invoice_urls
from fakturek.settings import get_settings


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


def _setup_sqlite_app(monkeypatch, tmp_path):
    db_path = tmp_path / "phase43.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import Contact, Invoice, Subject, SubjectBankAccount, User, UserSubject

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        db.add_all(
            [
                Subject(
                    id=1,
                    name="Seller One s.r.o.",
                    email="owner@example.test",
                    public_username="seller-one",
                    city="Praha",
                    country="CZ",
                    default_currency="CZK",
                    ico="12345678",
                ),
                Subject(
                    id=2,
                    name="Seller Two s.r.o.",
                    email="owner2@example.test",
                    public_username="seller-two",
                    city="Brno",
                    country="CZ",
                    default_currency="CZK",
                    ico="87654321",
                ),
                Contact(
                    id=1,
                    subject_id=1,
                    name="Acme Client a.s.",
                    email="billing@example.test",
                    city="Praha",
                    country="CZ",
                ),
                User(
                    id=1,
                    username="demo",
                    email="demo@example.test",
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
                SubjectBankAccount(
                    id=1,
                    subject_id=1,
                    label="Hlavní účet",
                    account_number="123456789/0100",
                    iban="CZ6508000000192000145399",
                    bic="GIBACZPX",
                    country="CZ",
                    is_default=True,
                    sort_order=1,
                ),
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    number="2026-0003",
                    status="issued",
                    invoice_style="modern",
                    issue_date=date(2026, 3, 1),
                    due_date=date(2026, 3, 15),
                    currency="CZK",
                    total_cents=12100,
                    buyer_name_cache="Acme Client a.s.",
                    public_token="tok-phase43",
                    bank_account_id=1,
                    bank_account_label="Hlavní účet",
                    bank_account_number="123456789/0100",
                    bank_account_iban="CZ6508000000192000145399",
                    bank_account_bic="GIBACZPX",
                    bank_account_country="CZ",
                ),
            ]
        )
        db.commit()

    app = create_app()
    client = TestClient(app)
    return client, SessionLocal


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "identifier": "demo",
            "password": "secret123",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_public_readable_preview_shows_back_link_for_logged_in_user(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )

    anonymous = client.get(public_urls["view"])
    assert anonymous.status_code == 200
    assert "← Zpět na fakturu" not in anonymous.text

    _login(client)
    logged_in = client.get(public_urls["view"])
    assert logged_in.status_code == 200
    assert 'href="/invoices/1"' in logged_in.text
    assert ">Zpět<" in logged_in.text

    _reset_settings_and_db()


def test_public_readable_preview_uses_invoice_document_layout(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )

    response = client.get(public_urls["view"])
    assert response.status_code == 200
    assert "invoice-style-modern" in response.text
    assert "Dodavatel" in response.text
    assert "Odběratel" in response.text
    assert "Přehled" in response.text
    assert "Platba" in response.text
    assert 'class="invoice-header"' in response.text
    assert 'class="pdf-doc"' not in response.text

    _reset_settings_and_db()


def test_public_invoice_access_updates_view_tracking(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    from fakturek.models import Invoice

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )

    preview = client.get(public_urls["view"])
    assert preview.status_code == 200
    pdf = client.get(public_urls["pdf"])
    assert pdf.status_code == 200

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert int(invoice.public_view_count or 0) >= 1
        assert invoice.public_first_viewed_at is not None
        assert invoice.public_last_viewed_at is not None

    _reset_settings_and_db()


def test_public_invoice_access_does_not_count_logged_in_subject_user(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    from fakturek.models import Invoice

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )

    _login(client)
    preview = client.get(public_urls["view"])
    assert preview.status_code == 200
    pdf = client.get(public_urls["pdf"])
    assert pdf.status_code == 200

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        assert int(invoice.public_view_count or 0) == 0
        assert invoice.public_first_viewed_at is None
        assert invoice.public_last_viewed_at is None

    _reset_settings_and_db()


def test_public_readable_preview_uses_subject_switch_link_when_other_subject_is_active(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    home = client.get("/")
    assert home.status_code == 200
    assert "/subjects/2/switch" in home.text
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert csrf_match is not None

    switch = client.post(
        "/subjects/2/switch",
        data={"next": "/settings", "csrf_token": csrf_match.group(1)},
        headers={"X-CSRF-Token": csrf_match.group(1)},
        follow_redirects=False,
    )
    assert switch.status_code == 303

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )
    preview = client.get(public_urls["view"])
    assert preview.status_code == 200
    assert 'href="/subjects/1/switch?next=%2Finvoices%2F1"' in preview.text

    _reset_settings_and_db()


def test_topbar_shows_current_subject_and_switches_from_home(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    home = client.get("/")
    assert home.status_code == 200
    assert "Seller One s.r.o." in home.text
    assert 'action="/subjects/2/switch"' in home.text

    _reset_settings_and_db()


def test_topbar_subject_switch_from_invoice_detail_uses_invoice_list_fallback(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert 'action="/subjects/2/switch"' in detail.text
    assert 'name="next" value="/invoices"' in detail.text

    _reset_settings_and_db()


def test_settings_bank_account_can_be_edited(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    settings_page = client.get("/settings?edit_account=1")
    assert settings_page.status_code == 200
    assert "Upravit účet" in settings_page.text
    assert "CZ6508000000192000145399" in settings_page.text

    response = client.post(
        "/settings/accounts/1/edit",
        data={
            "label": "Hlavní účet CZ",
            "account_number": "123456789/0100",
            "iban": "CZ6508000000192000145399",
            "bic": "GIBACZPX",
            "country": "CZ",
            "currency": "CZK",
            "is_default": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?saved=1")

    from fakturek.models import SubjectBankAccount

    with SessionLocal() as db:
        account = db.get(SubjectBankAccount, 1)
        assert account is not None
        assert account.label == "Hlavní účet CZ"
        assert account.iban == "CZ6508000000192000145399"

    _reset_settings_and_db()


def test_settings_can_create_subject_and_link_new_user(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    create_subject = client.post(
        "/settings/subjects/create",
        data={
            "name": "Klientovo IČO s.r.o.",
            "email": "klient@example.test",
            "phone": "",
            "street": "Ulice 5",
            "city": "Ostrava",
            "zip": "70030",
            "country": "CZ",
            "ico": "55554444",
            "dic": "CZ55554444",
            "default_currency": "CZK",
        },
        follow_redirects=False,
    )
    assert create_subject.status_code == 303
    assert create_subject.headers["location"].startswith("/settings?saved=1")

    from fakturek.models import Subject, User, UserSubject

    with SessionLocal() as db:
        new_subject = db.query(Subject).filter(Subject.name == "Klientovo IČO s.r.o.").one()
        owner_link = (
            db.query(UserSubject)
            .filter(UserSubject.user_id == 1, UserSubject.subject_id == int(new_subject.id))
            .one()
        )
        assert owner_link.role == "owner"
        assert new_subject.public_username
        new_subject_id = int(new_subject.id)

    create_user = client.post(
        f"/settings/subjects/{new_subject_id}/users/create",
        data={
            "username": "klient-admin",
            "email": "admin@klient.test",
            "password": "heslo1234567",
            "role": "manager",
            "can_view": "1",
            "can_edit": "1",
            "can_issue": "1",
        },
        follow_redirects=False,
    )
    assert create_user.status_code == 303
    assert create_user.headers["location"] == "/settings?saved=1#subjects-admin"

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "klient-admin").one()
        link = (
            db.query(UserSubject)
            .filter(UserSubject.user_id == int(user.id), UserSubject.subject_id == new_subject_id)
            .one()
        )
        assert link.role == "manager"
        assert link.can_view is True
        assert link.can_edit is True
        assert link.can_issue is True

    _reset_settings_and_db()


def test_settings_does_not_reflect_temporary_password_on_validation_error(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)
    temporary_password = "very-secret-temporary-password"

    response = client.post(
        "/settings/subjects/1/users/create",
        data={
            "username": "x",
            "email": "admin@example.test",
            "password": temporary_password,
            "role": "manager",
            "can_view": "1",
        },
    )

    assert response.status_code == 400
    assert temporary_password not in response.text
    assert 'name="password" type="password"' in response.text
    _reset_settings_and_db()


def test_settings_subject_lookup_prefills_fields_from_registry(monkeypatch, tmp_path):
    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)
    _login(client)

    from fakturek.company_lookup import CompanyPrefill
    from fakturek.models import Subject

    def _fake_lookup(db, ico, **kwargs):
        assert ico == "43847633"
        return (
            CompanyPrefill(
                name="ASP - Asistance, s. r. o.",
                street="Mierová 83",
                city="Bratislava",
                zip="82105",
                country="SK",
                ico="43847633",
                dic="SK2022456789",
            ),
            "cache",
            "orsr",
        )

    monkeypatch.setattr("fakturek.company_lookup.lookup_sk_company_prefill_with_cache", _fake_lookup)

    response = client.post(
        "/settings/subjects/create",
        data={
            "name": "",
            "email": "",
            "phone": "",
            "street": "",
            "city": "",
            "zip": "",
            "country": "SK",
            "ico": "43847633",
            "dic": "",
            "default_currency": "CZK",
            "lookup_registry": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Načteno ze slovenského registru ORSR (cache)." in response.text

    with SessionLocal() as db:
        assert db.query(Subject).filter(Subject.ico == "43847633").count() == 0

    _reset_settings_and_db()


def test_invoice_detail_hides_payment_qr_block(monkeypatch, tmp_path):
    client, _SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    detail = client.get("/invoices/1")
    assert detail.status_code == 200
    assert "Přehled dokladu" in detail.text
    assert "Variabilní symbol" in detail.text
    assert "Kopírovat VS" in detail.text
    assert "QR platba (ČR)" not in detail.text
    assert "PAY by square" not in detail.text
    assert "Naskenuj v mobilním bankovnictví." not in detail.text

    _reset_settings_and_db()


def test_country_filtered_qr_and_fallback_pdf_embed_image():
    account = BankAccountPayload(
        label="CZ účet",
        number="123456789/0100",
        iban="CZ6508000000192000145399",
        bic="GIBACZPX",
        country="CZ",
    )

    cz_qr = build_payment_qr_codes(
        account=account,
        amount_cents=12100,
        currency="CZK",
        beneficiary_name="Seller One s.r.o.",
        invoice_number="2026-0003",
        due_date=date(2026, 3, 15),
        subject_country="CZ",
    )
    assert [qr.kind for qr in cz_qr] == ["cz_spd"]

    pdf_bytes = render_invoice_pdf_bytes(
        InvoicePDFData(
            number="2026-0003",
            status="issued",
            issue_date=date(2026, 3, 1),
            taxable_supply_date=None,
            due_date=date(2026, 3, 15),
            currency="CZK",
            items_total_cents=12100,
            discount_cents=0,
            rounding_adjustment_cents=0,
            total_cents=12100,
            notes="Test PDF s QR",
            issuer={
                "name": "Seller One s.r.o.",
                "street": "Dlouhá 1",
                "city": "Praha",
                "zip": "11000",
                "country": "CZ",
                "ico": "12345678",
                "dic": "CZ12345678",
            },
            customer={
                "name": "Acme Client a.s.",
                "street": "Krátká 2",
                "city": "Praha",
                "zip": "12000",
                "country": "CZ",
                "ico": "87654321",
                "dic": "CZ87654321",
            },
            items=[
                {
                    "description": "Členský příspěvek",
                    "quantity": "1",
                    "unit_price_cents": 12100,
                    "vat_rate": "0",
                    "line_total_cents": 12100,
                }
            ],
            payment_account={
                "label": account.label,
                "number": account.number,
                "display": account.number,
                "iban": account.iban,
                "bic": account.bic,
                "country": account.country,
            },
            payment_qr_codes=[
                {
                    "kind": qr.kind,
                    "title": qr.title,
                    "payload": qr.payload,
                    "image_data_uri": qr.image_data_uri,
                }
                for qr in cz_qr
            ],
        )
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert b"/Subtype /Image" in pdf_bytes


def test_public_pdf_for_cash_shows_footer_without_bank_block(monkeypatch, tmp_path):
    pypdf = pytest.importorskip("pypdf")

    client, SessionLocal = _setup_sqlite_app(monkeypatch, tmp_path)

    from fakturek.models import Invoice

    with SessionLocal() as db:
        invoice = db.get(Invoice, 1)
        assert invoice is not None
        invoice.payment_method = "cash"
        invoice.footer_mode = "trade_register"
        invoice.footer_text = "Fyzická osoba zapsaná v živnostenském rejstříku."
        db.commit()

    public_urls = build_public_invoice_urls(
        public_username="seller-one",
        token="tok-phase43",
        invoice_number="2026-0003",
        invoice_id=1,
        secret_key="test-secret",
    )

    response = client.get(public_urls["pdf"])

    assert response.status_code == 200
    assert response.headers.get("content-type", "").lower().startswith("application/pdf")

    reader = pypdf.PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert "Hotově" in text
    assert "123456789/0100" not in text
    assert "QR platba" not in text
    assert "Fyzická osoba zapsaná v živnostenském rejstříku." in text

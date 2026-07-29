from __future__ import annotations

import os
import re
import socket
import threading
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
import uvicorn

sqlalchemy = pytest.importorskip("sqlalchemy")
playwright_sync = pytest.importorskip("playwright.sync_api")

import fakturek.db as db_module
from fakturek.auth import hash_password
from fakturek.db import Base
from fakturek.settings import get_settings

pytestmark = pytest.mark.playwright


def _reset_settings_and_db() -> None:
    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionLocal = None


class _UvicornThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(config)

    def run(self) -> None:  # pragma: no cover - exercised by browser test
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _start_server(app) -> tuple[str, _UvicornThread]:
    import httpx

    port = _free_port()
    server = _UvicornThread(app, port)
    server.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=1.0)
            if response.status_code == 200:
                return base_url, server
        except Exception as exc:  # pragma: no cover - diagnostic only
            last_error = exc
        time.sleep(0.1)
    server.stop()
    raise RuntimeError(f"Test server did not start: {last_error}")


def _setup_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / "playwright-smoke.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("CSRF_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "playwright-test-secret")
    monkeypatch.setenv("SIGNUP_ENABLED", "1")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Fakturek test")
    _reset_settings_and_db()

    from fakturek.db import get_engine, get_sessionmaker
    from fakturek.main import create_app
    from fakturek.models import (
        Contact,
        InvoiceSeries,
        Subject,
        SubjectBankAccount,
        User,
        UserSubject,
    )

    engine = get_engine()
    Base.metadata.create_all(engine)

    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        subject = Subject(
            id=1,
            name="Smoke Labs s.r.o.",
            email="owner@example.test",
            street="Testovaci 1",
            city="Praha",
            zip="11000",
            country="CZ",
            ico="12345678",
            dic="CZ12345678",
            is_vat_payer=True,
            default_currency="CZK",
            public_username="smoke-labs",
        )
        user = User(
            id=1,
            username="smoke-owner",
            email="smoke-owner@example.test",
            password_hash=hash_password("secret123", iterations=1_000),
            is_active=True,
            email_verified_at=date.today(),
        )
        db.add(subject)
        db.add(user)
        db.flush()
        db.add(
            UserSubject(
                user_id=int(user.id),
                subject_id=int(subject.id),
                role="owner",
                can_view=True,
                can_edit=True,
                can_issue=True,
                can_export=True,
            )
        )
        db.add(
            Contact(
                subject_id=int(subject.id),
                name="Existing Client s.r.o.",
                email="client@example.test",
                street="Klientska 2",
                city="Brno",
                zip="60200",
                country="CZ",
                ico="87654321",
                dic="CZ87654321",
            )
        )
        db.add(
            InvoiceSeries(
                subject_id=int(subject.id),
                name="default",
                prefix=f"{date.today().year}-",
                pad_length=4,
                last_counter=0,
                last_counter_year=date.today().year,
            )
        )
        db.add(
            SubjectBankAccount(
                subject_id=int(subject.id),
                label="Test bank",
                account_number="123456789/2010",
                iban="CZ6508000000192000145399",
                bic="GIBACZPX",
                currency="CZK",
                is_default=True,
            )
        )
        db.commit()

    return create_app(), SessionLocal


def _safe_screenshot_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name).strip("-")


def test_core_app_flow_in_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app, SessionLocal = _setup_app(monkeypatch, tmp_path)
    base_url, server = _start_server(app)

    artifact_root = Path(os.getenv("PLAYWRIGHT_ARTIFACT_DIR") or tmp_path / "playwright-artifacts")
    artifact_root.mkdir(parents=True, exist_ok=True)
    video_dir = artifact_root / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    try:
        with playwright_sync.sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                record_video_dir=str(video_dir),
            )
            page = context.new_page()

            def screenshot(name: str) -> None:
                page.screenshot(path=str(artifact_root / f"{_safe_screenshot_name(name)}.png"), full_page=True)

            page.goto(f"{base_url}/login")
            page.fill('input[name="identifier"]', "smoke-owner")
            page.fill('input[name="password"]', "secret123")
            page.get_by_role("button", name="Přihlásit").click()
            page.wait_for_url(f"{base_url}/")
            page.get_by_role("heading", name="Fakturek").wait_for()
            screenshot("01-dashboard")

            page.goto(f"{base_url}/contacts/new")
            page.fill('input[name="name"]', "Playwright Client s.r.o.")
            page.fill('input[name="email"]', "playwright-client@example.test")
            page.fill('input[name="street"]', "Browserova 42")
            page.fill('input[name="city"]', "Ostrava")
            page.fill('input[name="zip"]', "70030")
            page.select_option('select[name="country"]', "CZ")
            page.fill('input[name="ico"]', "11223344")
            page.get_by_role("button", name="Uložit kontakt").click()
            page.wait_for_url(lambda url: "/contacts/" in url and not url.endswith("/new"))
            page.get_by_text("Playwright Client s.r.o.").wait_for()
            screenshot("02-contacts")

            page.goto(f"{base_url}/invoices/new")
            page.select_option('select[name="contact_id"]', label="Playwright Client s.r.o.")
            page.fill('input[name="item_description"]', "Playwright smoke service")
            page.fill('input[name="item_quantity"]', "2")
            page.fill('input[name="item_unit"]', "hod")
            page.fill('input[name="item_unit_price"]', "1500")
            page.select_option('select[name="invoice_language"]', "cs")
            page.get_by_role("button", name="Vystavit fakturu").click()
            page.wait_for_url(lambda url: "/invoices/" in url and not url.endswith("/new"))
            page.get_by_role("heading", name=re.compile(rf"^{date.today().year}-")).wait_for()
            page.get_by_text("Playwright Client s.r.o.").first.wait_for()
            detail_url = page.url
            screenshot("03-invoice-detail")

            page.get_by_role("link", name="Upravit fakturu").click()
            page.wait_for_url(lambda url: url.endswith("/edit"))
            page.get_by_role("button", name="Upravit").first.click()
            page.locator('input[name="buyer_name"]').fill("Playwright Snapshot s.r.o.")
            page.locator('input[name="buyer_street"]').fill("Snapshotova 7")
            assert page.locator('input[name="seller_name"]').count() == 0
            page.get_by_role("button", name="Hotovo").click()
            page.locator('input[name="item_description"]').fill("Changed smoke service")
            page.get_by_role("button", name="Uložit změny").click()
            page.wait_for_url(detail_url)
            page.get_by_text("Playwright Snapshot s.r.o.").first.wait_for()
            page.get_by_text("Changed smoke service").wait_for()
            screenshot("04-invoice-buyer-override")

            page.get_by_role("button", name="Kopírovat odkaz").wait_for()
            pdf_response = page.request.get(f"{detail_url}/pdf")
            assert pdf_response.status == 200
            assert "application/pdf" in (pdf_response.headers.get("content-type") or "")

            public_href = page.locator('a:has-text("Sdílený odkaz")').get_attribute("href")
            assert public_href
            page.goto(public_href if public_href.startswith("http") else f"{base_url}{public_href}")
            page.get_by_text("Playwright Snapshot s.r.o.").first.wait_for()
            screenshot("05-public-preview")

            for path, heading in [
                ("/invoices", "Faktury"),
                ("/payments", "Platby"),
                ("/stats", "Statistiky"),
                ("/imports", "Export a import dat"),
                ("/settings", "Nastavení"),
            ]:
                page.goto(f"{base_url}{path}")
                page.get_by_role("heading", name=heading).first.wait_for()
                if path == "/admin":
                    page.get_by_role("button", name="Statistiky").click()
                    page.get_by_text("Posledních 6 měsíců").wait_for()
                    page.get_by_text("Přihlášení a účty").wait_for()
            screenshot("06-settings-or-admin")

            mobile = browser.new_context(viewport={"width": 390, "height": 844})
            mobile_page = mobile.new_page()
            mobile_page.goto(f"{base_url}/login")
            mobile_page.fill('input[name="identifier"]', "smoke-owner")
            mobile_page.fill('input[name="password"]', "secret123")
            mobile_page.get_by_role("button", name="Přihlásit").click()
            mobile_page.wait_for_url(f"{base_url}/")
            mobile_page.get_by_role("heading", name="Fakturek").wait_for()
            mobile_page.screenshot(path=str(artifact_root / "07-mobile-dashboard.png"), full_page=True)
            mobile.close()

            context.close()
            browser.close()

        from fakturek.models import Invoice, InvoiceParty

        with SessionLocal() as db:
            invoice = db.query(Invoice).filter(Invoice.buyer_name_cache == "Playwright Snapshot s.r.o.").one()
            buyer = (
                db.query(InvoiceParty)
                .filter(InvoiceParty.invoice_id == int(invoice.id), InvoiceParty.role == "buyer")
                .one()
            )
            seller_count = (
                db.query(InvoiceParty)
                .filter(InvoiceParty.invoice_id == int(invoice.id), InvoiceParty.role == "seller")
                .count()
            )
            assert buyer.street == "Snapshotova 7"
            assert seller_count == 1
    finally:
        server.stop()
        _reset_settings_and_db()

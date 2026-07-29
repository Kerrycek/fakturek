from __future__ import annotations

import os
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError

from test_playwright_smoke import _reset_settings_and_db, _safe_screenshot_name, _setup_app, _start_server

pytestmark = pytest.mark.playwright


def _launch_chromium_or_skip(p):
    try:
        return p.chromium.launch()
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            pytest.skip("Playwright Chromium browser is not installed in this environment")
        raise


def _assert_no_document_horizontal_scroll(page) -> None:
    metrics = page.evaluate(
        """
        () => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
          bodyScrollWidth: document.body ? document.body.scrollWidth : 0,
        })
        """
    )
    assert metrics["scrollWidth"] <= metrics["clientWidth"] + 2, metrics
    assert metrics["bodyScrollWidth"] <= metrics["clientWidth"] + 2, metrics


def _login(page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill('input[name="identifier"]', "smoke-owner")
    page.fill('input[name="password"]', "secret123")
    page.get_by_role("button", name="Přihlásit").click()
    page.wait_for_url(f"{base_url}/")


def test_phase6_responsive_core_routes_have_no_unintended_horizontal_scroll(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Browser smoke for the application's 320px+ responsive contract."""

    artifact_root = Path(os.getenv("PLAYWRIGHT_ARTIFACT_DIR") or tmp_path / "phase6-responsive-artifacts")
    artifact_root.mkdir(parents=True, exist_ok=True)

    with playwright_sync.sync_playwright() as p:
        browser = _launch_chromium_or_skip(p)

        app, _SessionLocal = _setup_app(monkeypatch, tmp_path)
        base_url, server = _start_server(app)
        try:
            routes = [
                ("/", "dashboard"),
                ("/invoices", "invoices"),
                ("/invoices/new", "invoice-editor"),
                ("/settings", "settings"),
            ]
            for width, height in [(320, 740), (390, 844), (768, 1024)]:
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                _login(page, base_url)
                for path, name in routes:
                    response = page.goto(f"{base_url}{path}")
                    assert response is None or response.status < 400
                    page.locator("body").wait_for()
                    _assert_no_document_horizontal_scroll(page)
                    page.screenshot(
                        path=str(artifact_root / f"{width}px-{_safe_screenshot_name(name)}.png"),
                        full_page=True,
                    )
                context.close()
        finally:
            browser.close()
            server.stop()
            _reset_settings_and_db()

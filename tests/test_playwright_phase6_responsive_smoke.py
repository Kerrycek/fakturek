from __future__ import annotations

import os
from datetime import date
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
          overflowElements: Array.from(document.querySelectorAll('body *'))
            .map((element) => {
              const rect = element.getBoundingClientRect();
              return {
                tag: element.tagName,
                className: String(element.className || '').slice(0, 120),
                left: Math.round(rect.left * 10) / 10,
                right: Math.round(rect.right * 10) / 10,
                width: Math.round(rect.width * 10) / 10,
              };
            })
            .filter((item) => item.width > 0 && item.right > document.documentElement.clientWidth + 2)
            .slice(0, 12),
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

        app, SessionLocal = _setup_app(monkeypatch, tmp_path)
        from fakturek.models import Invoice

        with SessionLocal() as db:
            db.add(
                Invoice(
                    id=1,
                    subject_id=1,
                    contact_id=1,
                    series_id=1,
                    number=f"{date.today().year}-0001",
                    status="issued",
                    issue_date=date.today(),
                    due_date=date.today(),
                    currency="CZK",
                    total_cents=125_000,
                    buyer_name_cache="Existing Client s.r.o.",
                )
            )
            db.commit()
        base_url, server = _start_server(app)
        try:
            routes = [
                ("/", "dashboard"),
                ("/invoices", "invoices"),
                ("/invoices/new", "invoice-editor"),
                ("/settings", "settings"),
            ]
            for width, height in [
                (320, 740),
                (360, 800),
                (390, 844),
                (768, 1024),
                (901, 800),
            ]:
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                response = page.goto(f"{base_url}/password/reset")
                assert response is None or response.status < 400
                page.locator("body").wait_for()
                login_link = page.get_by_role("link", name="Přihlásit", exact=True)
                assert login_link.is_visible()
                login_box = login_link.bounding_box()
                assert login_box is not None
                assert login_box["x"] >= -1
                assert login_box["x"] + login_box["width"] <= width + 1
                _assert_no_document_horizontal_scroll(page)
                page.screenshot(
                    path=str(artifact_root / f"{width}px-public-password-reset.png"),
                    full_page=True,
                )
                _login(page, base_url)
                for path, name in routes:
                    response = page.goto(f"{base_url}{path}")
                    assert response is None or response.status < 400
                    page.locator("body").wait_for()
                    _assert_no_document_horizontal_scroll(page)
                    if width == 901 and path == "/invoices":
                        table = page.locator(".invoice-list-mobile-table")
                        assert table.locator("tbody tr").count() == 1
                        table.locator(".row-actions-trigger").click()
                        panel = table.locator(".row-actions-panel")
                        panel.wait_for(state="visible")
                        panel_box = panel.bounding_box()
                        assert panel_box is not None
                        assert panel_box["x"] >= -1
                        assert panel_box["x"] + panel_box["width"] <= width + 1
                        assert panel_box["y"] >= -1
                        assert panel_box["y"] + panel_box["height"] <= height + 1
                        center = [
                            panel_box["x"] + panel_box["width"] / 2,
                            panel_box["y"] + panel_box["height"] / 2,
                        ]
                        assert page.evaluate(
                            """
                            ([x, y]) => {
                              const panel = document.querySelector(
                                '.row-actions-menu[open] .row-actions-panel'
                              );
                              const hit = document.elementFromPoint(x, y);
                              return Boolean(panel && hit && (hit === panel || panel.contains(hit)));
                            }
                            """,
                            center,
                        )
                    page.screenshot(
                        path=str(artifact_root / f"{width}px-{_safe_screenshot_name(name)}.png"),
                        full_page=True,
                    )
                context.close()
        finally:
            browser.close()
            server.stop()
            _reset_settings_and_db()

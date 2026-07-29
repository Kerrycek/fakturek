from __future__ import annotations

import re
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

pytestmark = pytest.mark.playwright


def _extract_topbar_controller_script() -> str:
    template = Path("templates/base.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script data-fakturek-topbar-dropdown-controller>(.*?)</script>',
        template,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_topbar_dropdowns_toggle_and_close_on_outside_click() -> None:
    script = _extract_topbar_controller_script()
    html = f"""
    <!doctype html>
    <html lang="cs">
      <body>
        <button id="outside">Mimo menu</button>
        <details id="theme" data-topbar-menu>
          <summary id="theme-summary"><span>Vzhled</span></summary>
          <div class="dropdown-menu"><button id="theme-inside">Světlý</button></div>
        </details>
        <details id="subject" data-topbar-menu>
          <summary id="subject-summary"><span>Uživatel</span></summary>
          <div class="dropdown-menu"><button id="subject-inside">Organizace</button></div>
        </details>
        <script>{script}</script>
      </body>
    </html>
    """

    with playwright_sync.sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")

            page.click("#theme-summary")
            assert page.locator("#theme").evaluate("node => node.open") is True
            expect(page.locator("#theme-summary")).to_have_attribute("aria-expanded", "true")

            page.click("#theme-summary")
            assert page.locator("#theme").evaluate("node => node.open") is False
            expect(page.locator("#theme-summary")).to_have_attribute("aria-expanded", "false")

            page.click("#theme-summary")
            page.click("#subject-summary")
            assert page.locator("#theme").evaluate("node => node.open") is False
            assert page.locator("#subject").evaluate("node => node.open") is True

            page.click("#subject-inside")
            assert page.locator("#subject").evaluate("node => node.open") is True

            page.click("#outside")
            assert page.locator("#subject").evaluate("node => node.open") is False

            page.click("#subject-summary")
            assert page.locator("#subject").evaluate("node => node.open") is True
            page.keyboard.press("Escape")
            assert page.locator("#subject").evaluate("node => node.open") is False
        finally:
            browser.close()

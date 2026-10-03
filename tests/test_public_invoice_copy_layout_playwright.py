from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from test_playwright_smoke import _reset_settings_and_db, _setup_app, _start_server

playwright_sync = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.playwright


def _setup_public_invoice(monkeypatch, tmp_path):
    app, session_factory = _setup_app(monkeypatch, tmp_path)
    from fakturek.models import Invoice, InvoiceItem

    with session_factory() as db:
        db.add(
            Invoice(
                id=1,
                subject_id=1,
                contact_id=1,
                series_id=1,
                bank_account_id=1,
                bank_account_number="123456789/2010",
                bank_account_iban="CZ6508000000192000145399",
                bank_account_bic="GIBACZPX",
                bank_account_country="CZ",
                number="2026-TEST-0001",
                public_token="synthetic-copy-layout",
                status="issued",
                issue_date=date(2026, 10, 3),
                due_date=date(2026, 10, 17),
                currency="CZK",
                total_cents=125_000,
                variable_symbol="20260001",
                buyer_name_cache="Synthetic Client s.r.o.",
            )
        )
        db.flush()
        db.add(
            InvoiceItem(
                invoice_id=1,
                description="Synthetic layout test – no real invoice",
                quantity=1,
                unit_price_cents=125_000,
                vat_rate=0,
                line_total_cents=125_000,
            )
        )
        db.commit()
    return app


@pytest.mark.parametrize("browser_name", ["chromium", "webkit"])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_public_payment_copy_buttons_share_label_row(
    monkeypatch, tmp_path, browser_name, color_scheme
):
    app = _setup_public_invoice(monkeypatch, tmp_path)
    base_url, server = _start_server(app)
    try:
        with playwright_sync.sync_playwright() as playwright:
            try:
                browser = getattr(playwright, browser_name).launch()
            except playwright_sync.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip(f"Playwright {browser_name} is not installed")
                raise
            context = browser.new_context(color_scheme=color_scheme)
            # Capture clipboard writes without changing the operating-system clipboard.
            context.add_init_script("""
                window.copiedValues = [];
                Object.defineProperty(navigator, 'clipboard', {value: {
                  writeText: async (value) => { window.copiedValues.push(value); }
                }});
            """)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("response", lambda res: errors.append(res.url) if res.status >= 500 else None)
            path = "/smoke-labs/i/synthetic-copy-layout/2026-TEST-0001"
            response = page.goto(base_url + path)
            assert response.status == 200
            assert page.url == base_url + path
            assert page.title() == "Faktura 2026-TEST-0001 – tisk"
            assert page.locator("html").get_attribute("data-public-invoice-theme") == color_scheme
            assert page.locator(".payment-row .public-copy-button").count() == 4

            for width in [320, 390, 768, 1024, 1440]:
                page.set_viewport_size({"width": width, "height": 844})
                page.evaluate("() => document.fonts.ready")
                rows = page.locator(".payment-row").evaluate_all("""
                    rows => rows.map(row => {
                      const rect = el => {
                        const r = el.getBoundingClientRect();
                        return {x:r.x, y:r.y, width:r.width, height:r.height, bottom:r.bottom};
                      };
                      return {label: rect(row.children[0]), value: rect(row.children[1]),
                        button: rect(row.querySelector('.public-copy-button'))};
                    })
                """)
                for row in rows:
                    label, value, button = row["label"], row["value"], row["button"]
                    assert abs(label["y"] - button["y"]) <= 1, (width, row)
                    assert abs(label["height"] - button["height"]) <= 1, (width, row)
                    assert 24 <= button["width"] <= 25, (width, row)
                    assert 24 <= button["height"] <= 25, (width, row)
                    assert value["y"] >= button["bottom"], (width, row)
                    assert abs(label["x"] - value["x"]) <= 1, (width, row)
                assert max(row["button"]["x"] for row in rows) - min(
                    row["button"]["x"] for row in rows
                ) <= 1
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                )
                if artifact_dir := os.getenv("PLAYWRIGHT_ARTIFACT_DIR"):
                    artifact_root = Path(artifact_dir)
                    artifact_root.mkdir(parents=True, exist_ok=True)
                    name = f"public-copy-{browser_name}-{color_scheme}-{width}"
                    page.screenshot(path=str(artifact_root / f"{name}.png"), full_page=True)
                    page.locator(".payment-card").screenshot(
                        path=str(artifact_root / f"{name}-payment.png")
                    )

            values = ["123456789/2010", "20260001", "GIBACZPX", "CZ6508000000192000145399"]
            buttons = page.locator(".payment-row .public-copy-button")
            for index, value in enumerate(values):
                button = buttons.nth(index)
                original_label = button.get_attribute("aria-label")
                button.focus()
                button.press("Enter")
                playwright_sync.expect(button).to_have_attribute("aria-label", "Zkopírováno")
                assert page.evaluate("window.copiedValues.at(-1)") == value
                playwright_sync.expect(button).to_have_attribute("aria-label", original_label)
            assert not errors
            context.close()
            browser.close()
    finally:
        server.stop()
        server.join(timeout=5)
        _reset_settings_and_db()

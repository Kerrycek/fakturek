from __future__ import annotations

import io
from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

playwright_sync = pytest.importorskip("playwright.sync_api")
pypdf = pytest.importorskip("pypdf")
from playwright.sync_api import Error as PlaywrightError

from fakturek.banking import BankAccountPayload, build_payment_qr_codes, format_iban_for_display
from fakturek.money import format_cents, format_quantity

pytestmark = pytest.mark.playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _render_invoice_pdf_html(*, invoice_style: str = "modern") -> str:
    env = Environment(
        loader=FileSystemLoader(str(PROJECT_ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["money"] = format_cents
    env.filters["quantity"] = format_quantity
    env.filters["iban_display"] = format_iban_for_display
    env.filters["invoice_payment_method"] = lambda value: {
        "bank_transfer": "Převodem",
        "cash": "Hotově",
        "card": "Kartou",
        "cod": "Dobírkou",
    }.get(str(value or ""), str(value or ""))
    env.filters["invoice_status"] = lambda value: {
        "draft": "Koncept",
        "issued": "Vystavená",
        "sent": "Odeslaná",
        "paid": "Zaplacená",
        "cancelled": "Stornováno",
    }.get(str(value or ""), str(value or ""))

    account = BankAccountPayload(
        label="Fio účet",
        number="2200041594/2010",
        iban="CZ0420100000002200041594",
        bic="FIOBCZPPXXX",
        country="CZ",
    )
    qrs = build_payment_qr_codes(
        account=account,
        amount_cents=2_640_000,
        currency="CZK",
        beneficiary_name="vpsFree.cz, z.s.",
        invoice_number="2026-0596",
        variable_symbol="2187",
        due_date=None,
        subject_country="CZ",
    )

    invoice = SimpleNamespace(
        number="2026-0596",
        status="paid",
        issue_date=date(2026, 7, 3),
        due_date=date(2026, 7, 10),
        currency="CZK",
        total_cents=2_640_000,
        notes="",
        paid_on=None,
    )
    item = SimpleNamespace(
        description="Platba členského příspěvku za období 5.7.2026 až 5.1.2027",
        quantity="1.00",
        unit="",
        unit_price_cents=2_640_000,
        vat_rate="0",
        line_total_cents=2_640_000,
    )

    return env.get_template("invoices/print.html").render(
        {
            "pdf_mode": True,
            "public_mode": False,
            "preview_mode": True,
            "app_css": "",
            "back_url": None,
            "internal_pdf_url": None,
            "internal_pdf_download_url": None,
            "internal_isdoc_download_url": None,
            "public_pdf_url": None,
            "public_pdf_download_url": None,
            "public_isdoc_download_url": None,
            "invoice": invoice,
            "document_label": "Faktura",
            "document_kicker": "Faktura",
            "source_invoice_number": None,
            "seller": {
                "name": "vpsFree.cz, z.s.",
                "street": "Nad Dalejským údolím 2699/9",
                "zip": "15500",
                "city": "Praha",
                "country": "CZ",
                "ico": "26568055",
                "dic": "CZ26568055",
                "phone": "",
            },
            "buyer": {
                "name": "Bookbot s.r.o.",
                "street": "Dukelských hrdinů 359/21",
                "zip": "17000",
                "city": "Praha",
                "country": "CZ",
                "ico": "05400651",
                "dic": "CZ05400651",
                "phone": "",
            },
            "items": [item],
            "items_total_cents": 2_640_000,
            "discount_cents": 0,
            "rounding_adjustment_cents": 0,
            "show_vat": False,
            "is_vat_payer": False,
            "taxable_supply_date": None,
            "payment_method": "bank_transfer",
            "payment_method_label": "Převodem",
            "payment_account": account,
            "payment_qr_codes": qrs,
            "variable_symbol": "2187",
            "footer_text": "Spolek zapsaný ve spolkovém rejstříku.",
            "invoice_language": "cs",
            "invoice_style": invoice_style,
            "invoice_i18n": {},
            "status_label": "Zaplacená",
        }
    )


def _launch_chromium_or_skip(p):
    try:
        return p.chromium.launch(headless=True)
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            pytest.skip("Playwright Chromium browser is not installed in this environment")
        raise


def test_invoice_pdf_payment_layout_has_qr_inside_and_clean_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    html = _render_invoice_pdf_html()

    with playwright_sync.sync_playwright() as p:
        browser = _launch_chromium_or_skip(p)
        page = browser.new_page(viewport={"width": 1323, "height": 1870}, device_scale_factor=1)
        page.set_content(html, wait_until="load", timeout=30_000)
        page.locator(".pdf-doc").wait_for(state="visible")

        doc_text = page.locator(".pdf-doc").inner_text()
        assert "Celkem k úhradě" not in doc_text
        assert "CZ04 2010 0000 0022 0004 1594" in doc_text
        assert "CZ0420100000002200041594" not in doc_text

        assert page.locator(".pdf-status-block").count() == 0

        payment_text = page.locator(".pdf-payment-wide").inner_text()
        assert "PLATEBNÍ ÚDAJE" in payment_text
        assert doc_text.count("Způsob platby") == 1
        assert doc_text.count("Variabilní symbol") == 1
        assert "1.00" not in doc_text
        assert page.locator(".pdf-item-meta").first.inner_text().strip().startswith("1 ")
        assert payment_text.index("Způsob platby") < payment_text.index("Číslo účtu")
        assert payment_text.index("Číslo účtu") < payment_text.index("Variabilní symbol")
        assert payment_text.index("BIC / SWIFT") < payment_text.index("IBAN")

        geometry = page.evaluate(
            """
            () => {
              const paymentBlock = document.querySelector('.pdf-payment-wide');
              const qr = document.querySelector('.pdf-payment-qr-inline');
              const ibanRow = document.querySelector('.pdf-payment-row.value-iban');
              const ibanValue = ibanRow?.querySelector('span:last-child');
              const detailGrid = document.querySelector('.pdf-payment-details');
              const columns = Array.from(document.querySelectorAll('.pdf-payment-col'));
              if (!paymentBlock || !qr || !ibanRow || !ibanValue || !detailGrid || columns.length < 2) return null;
              const block = paymentBlock.getBoundingClientRect();
              const qrBox = qr.getBoundingClientRect();
              const iban = ibanValue.getBoundingClientRect();
              const grid = detailGrid.getBoundingClientRect();
              const leftColumn = columns[0].getBoundingClientRect();
              const rightColumn = columns[1].getBoundingClientRect();
              const typeStyles = Array.from(document.querySelectorAll('.pdf-payment-row span'))
                .map((el) => {
                  const style = window.getComputedStyle(el);
                  return {
                    text: el.textContent.trim(),
                    fontFamily: style.fontFamily,
                    fontSize: style.fontSize,
                    lineHeight: style.lineHeight,
                    whiteSpace: style.whiteSpace,
                  };
                });
              return {
                block: { left: block.left, top: block.top, right: block.right, bottom: block.bottom },
                qr: { left: qrBox.left, top: qrBox.top, right: qrBox.right, bottom: qrBox.bottom },
                iban: { left: iban.left, top: iban.top, right: iban.right, bottom: iban.bottom },
                grid: { left: grid.left, top: grid.top, right: grid.right, bottom: grid.bottom },
                leftColumn: { left: leftColumn.left, right: leftColumn.right, width: leftColumn.width },
                rightColumn: { left: rightColumn.left, right: rightColumn.right, width: rightColumn.width },
                typeStyles,
              };
            }
            """
        )
        assert geometry is not None
        assert geometry["qr"]["left"] >= geometry["block"]["left"] - 1
        assert geometry["qr"]["right"] <= geometry["block"]["right"] + 1
        assert geometry["qr"]["top"] >= geometry["block"]["top"] - 1
        assert geometry["qr"]["bottom"] <= geometry["block"]["bottom"] + 1
        assert geometry["qr"]["left"] - geometry["grid"]["right"] >= 10
        assert geometry["qr"]["left"] - geometry["iban"]["right"] >= 10
        assert geometry["iban"]["right"] <= geometry["grid"]["right"] + 1
        assert geometry["rightColumn"]["left"] - geometry["leftColumn"]["right"] >= 12
        assert geometry["rightColumn"]["left"] >= geometry["block"]["left"] + 500

        families = {item["fontFamily"] for item in geometry["typeStyles"]}
        value_spaces = {item["whiteSpace"] for item in geometry["typeStyles"] if item["text"] in {"Převodem", "2200041594/2010", "2187", "FIOBCZPPXXX", "CZ04 2010 0000 0022 0004 1594"}}
        assert value_spaces == {"nowrap"}
        assert len(families) == 1

        artifact_root = Path(os.getenv("PLAYWRIGHT_ARTIFACT_DIR") or tmp_path)
        artifact_root.mkdir(parents=True, exist_ok=True)
        page.locator(".pdf-doc").screenshot(path=str(artifact_root / "invoice-pdf-layout.png"))
        browser.close()


def test_multi_page_pdf_keeps_each_item_description_and_metadata_together():
    html = _render_invoice_pdf_html()

    with playwright_sync.sync_playwright() as p:
        browser = _launch_chromium_or_skip(p)
        page = browser.new_page(viewport={"width": 1323, "height": 1870}, device_scale_factor=1)
        page.set_content(html, wait_until="load", timeout=30_000)
        page.locator(".pdf-doc").wait_for(state="visible")
        page.locator(".pdf-items-list").evaluate(
            """
            (list) => {
              const template = list.querySelector('.pdf-item-row');
              list.replaceChildren();
              for (let index = 1; index <= 12; index += 1) {
                const row = template.cloneNode(true);
                row.querySelector('.pdf-item-title').textContent = `Auditni polozka ${index}`;
                row.querySelector('.pdf-item-meta').textContent = `DETAIL-${index}`;
                row.querySelector('.pdf-item-total').textContent = '6 050,00 CZK';
                list.appendChild(row);
              }
            }
            """
        )
        pdf_bytes = page.pdf(format="A4", print_background=True)
        browser.close()

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    assert len(page_texts) >= 2
    for index in range(1, 13):
        title_pages = [
            page_no
            for page_no, text in enumerate(page_texts)
            if f"Auditni polozka {index}" in text
        ]
        detail_pages = [
            page_no
            for page_no, text in enumerate(page_texts)
            if f"DETAIL-{index}" in text
        ]
        assert title_pages == detail_pages
        assert len(title_pages) == 1


@pytest.mark.parametrize("invoice_style", ["modern", "classic", "minimal"])
def test_invoice_pdf_payment_layout_is_stable_across_invoice_styles(invoice_style: str, tmp_path: Path):
    html = _render_invoice_pdf_html(invoice_style=invoice_style)

    with playwright_sync.sync_playwright() as p:
        browser = _launch_chromium_or_skip(p)
        page = browser.new_page(viewport={"width": 1323, "height": 1870}, device_scale_factor=1)
        page.set_content(html, wait_until="load", timeout=30_000)
        page.locator(".pdf-doc").wait_for(state="visible")

        body_class = page.locator("body").get_attribute("class") or ""
        assert f"invoice-style-{invoice_style}" in body_class

        doc_text = page.locator(".pdf-doc").inner_text()
        assert page.locator(".pdf-status-block").count() == 0
        assert doc_text.count("Způsob platby") == 1
        assert doc_text.count("Variabilní symbol") == 1
        assert "Doklad\nUhrazeno" not in doc_text
        assert "1.00" not in doc_text
        assert page.locator(".pdf-item-meta").first.inner_text().strip().startswith("1 ")

        geometry = page.evaluate(
            """
            () => {
              const block = document.querySelector('.pdf-payment-wide')?.getBoundingClientRect();
              const qr = document.querySelector('.pdf-payment-qr-inline')?.getBoundingClientRect();
              const detail = document.querySelector('.pdf-payment-details')?.getBoundingClientRect();
              const iban = document.querySelector('.pdf-payment-row.value-iban span:last-child')?.getBoundingClientRect();
              const overview = document.querySelector('.pdf-overview-grid')?.getBoundingClientRect();
              const parties = document.querySelector('.pdf-parties')?.getBoundingClientRect();
              if (!block || !qr || !detail || !iban || !overview || !parties) return null;
              return {
                block: { left: block.left, right: block.right, top: block.top, bottom: block.bottom },
                qr: { left: qr.left, right: qr.right, top: qr.top, bottom: qr.bottom },
                detail: { left: detail.left, right: detail.right, top: detail.top, bottom: detail.bottom },
                iban: { left: iban.left, right: iban.right, top: iban.top, bottom: iban.bottom },
                overview: { left: overview.left, right: overview.right, top: overview.top, bottom: overview.bottom },
                parties: { left: parties.left, right: parties.right },
              };
            }
            """
        )
        assert geometry is not None
        assert geometry["overview"]["left"] == pytest.approx(geometry["parties"]["left"], abs=1)
        assert geometry["overview"]["right"] == pytest.approx(geometry["parties"]["right"], abs=1)
        assert geometry["qr"]["left"] >= geometry["block"]["left"] - 1
        assert geometry["qr"]["right"] <= geometry["block"]["right"] + 1
        assert geometry["qr"]["left"] - geometry["detail"]["right"] >= 10
        assert geometry["qr"]["left"] - geometry["iban"]["right"] >= 10

        artifact_root = Path(os.getenv("PLAYWRIGHT_ARTIFACT_DIR") or tmp_path)
        artifact_root.mkdir(parents=True, exist_ok=True)
        page.locator(".pdf-doc").screenshot(path=str(artifact_root / f"invoice-pdf-layout-{invoice_style}.png"))
        browser.close()

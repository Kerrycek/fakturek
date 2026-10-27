"""Print appearance contract: render real PDFs, including the public route mode.

Keep the approved invoice layout on plain white paper. Dependency upgrades must
not add web-card shadows/gradients or decoration outside the document. Requires
Poppler's pdftoppm (poppler-utils on Linux, poppler on macOS).
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import qrcode
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from pypdf import PdfReader

from fakturek import pdf as pdf_module
from fakturek.banking import BankAccountPayload, format_iban_for_display
from fakturek.money import format_cents, format_quantity
from fakturek.pdf import render_html_pdf_bytes

ROOT = Path(pdf_module.__file__).resolve().parents[1]


def render_print_contract_html(*, public: bool, style: str, paid: bool, count: int = 1) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    env.filters.update(
        money=format_cents,
        quantity=format_quantity,
        iban_display=format_iban_for_display,
        invoice_payment_method=lambda _: "Převodem",
        invoice_status=lambda _: "Zaplacená",
    )
    image = io.BytesIO()
    qrcode.make("https://example.invalid/test-not-a-payment").save(image, format="PNG")
    party = dict(
        street="Testovací 1",
        zip="10000",
        city="Praha",
        country="CZ",
        ico="00000000",
        dic="",
        phone="",
    )
    return env.get_template("invoices/print.html").render(
        pdf_mode=True,
        public_mode=public,
        preview_mode=True,
        app_css=(ROOT / "static/app.css").read_text(),
        invoice=NS(
            number="TEST-2026-0001",
            status="paid" if paid else "sent",
            issue_date=date(2026, 10, 27),
            due_date=date(2026, 11, 3),
            currency="CZK",
            total_cents=360000 * count,
            notes="",
            paid_on=None,
        ),
        document_label="Faktura",
        document_kicker="Faktura",
        source_invoice_number=None,
        seller=dict(party, name="Ukázkový dodavatel s.r.o."),
        buyer=dict(party, name="Ukázkový odběratel", street="Ukázková 2"),
        items=[
            NS(
                description=f"Testovací položka {i:02d}",
                quantity="1.00",
                unit="",
                unit_price_cents=360000,
                vat_rate="0",
                line_total_cents=360000,
            )
            for i in range(1, count + 1)
        ],
        items_total_cents=360000 * count,
        discount_cents=0,
        rounding_adjustment_cents=0,
        show_vat=False,
        is_vat_payer=False,
        taxable_supply_date=None,
        payment_method="bank_transfer",
        payment_method_label="Převodem",
        payment_account=BankAccountPayload(
            label="Test",
            number="0000000000/0000",
            iban="CZ0000000000000000000000",
            bic="TESTCZ00XXX",
            country="CZ",
        ),
        payment_qr_codes=[
            NS(
                image_data_uri="data:image/png;base64,"
                + base64.b64encode(image.getvalue()).decode(),
                title="QR ukázka (test)",
            )
        ],
        variable_symbol="99999999",
        footer_text="TESTOVACÍ UKÁZKA — NEPLATIT.",
        invoice_language="cs",
        invoice_style=style,
        invoice_i18n={},
        status_label="Zaplacená" if paid else "Odeslaná",
    )


def assert_plain_paper(pdf: bytes, tmp_path: Path) -> PdfReader:
    executable = shutil.which("pdftoppm")
    assert executable, (
        "Install poppler-utils (Linux) or poppler (macOS) to check the real PDF appearance"
    )
    path = tmp_path / "invoice.pdf"
    path.write_bytes(pdf)
    reader = PdfReader(io.BytesIO(pdf))
    subprocess.run(
        [executable, "-scale-to", "1500", "-png", str(path), str(tmp_path / "page")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    pages = sorted(tmp_path.glob("page-*.png"))
    assert len(pages) == len(reader.pages)
    for page in pages:
        with Image.open(page) as original:
            image = original.convert("RGB")
            width, height = image.size
            # The approved A4 output has blank outer 3.5% side margins. Shadows
            # in WeasyPrint 70 paint into these strips; the previous output does not.
            margin = int(width * 0.035)
            for box in ((0, 0, margin, height), (width - margin, 0, width, height)):
                assert all(low >= 254 for low, _ in image.crop(box).getextrema()), (
                    f"Decorative pixels outside approved invoice area: {page.name}"
                )
    return reader


@pytest.mark.parametrize("public", [True, False], ids=["public", "internal"])
@pytest.mark.parametrize("style", ["modern", "classic", "minimal"])
@pytest.mark.parametrize("paid", [True, False], ids=["paid", "unpaid"])
def test_invoice_pdf_preserves_plain_paper(public: bool, style: str, paid: bool, tmp_path: Path):
    html = render_print_contract_html(public=public, style=style, paid=paid)
    reader = assert_plain_paper(render_html_pdf_bytes(html, base_url=ROOT), tmp_path)
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text().replace("\u00a0", " ")
    for expected in ("TEST-2026-0001", "Testovací položka 01", "3 600,00 CZK", "99999999"):
        assert expected in text
    assert ("Tento doklad je již uhrazený" in text) is paid


def test_multi_page_public_pdf_preserves_plain_paper_and_items(tmp_path: Path):
    html = render_print_contract_html(public=True, style="modern", paid=False, count=40)
    reader = assert_plain_paper(render_html_pdf_bytes(html, base_url=ROOT), tmp_path)
    assert len(reader.pages) >= 2
    text = "\n".join(page.extract_text() for page in reader.pages)
    for i in range(1, 41):
        assert text.count(f"Testovací položka {i:02d}") == 1
    assert "144 000,00 CZK" in text.replace("\u00a0", " ")

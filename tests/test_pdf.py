from datetime import date
import io

from fastapi.testclient import TestClient
import pypdf

from fakturek.main import create_app
from fakturek.pdf import InvoicePDFData, render_invoice_pdf_bytes


def test_invoice_pdf_endpoint_returns_pdf_bytes_even_without_db():
    """The PDF export endpoint should exist and return application/pdf.

    We intentionally do not rely on a running DB in unit tests.
    The app should fall back to an error PDF when DB is unavailable.
    """

    client = TestClient(create_app())
    res = client.get("/invoices/1/pdf")
    assert res.status_code == 200

    # Content-Type can include charset, but for PDFs it should be application/pdf.
    assert res.headers.get("content-type", "").lower().startswith("application/pdf")

    # A valid PDF starts with the PDF magic header.
    assert res.content.startswith(b"%PDF")

    # Content-Disposition is set to inline with a .pdf filename.
    cd = res.headers.get("content-disposition", "")
    assert ".pdf" in cd.lower()


def _paid_document_pdf_text(*, document_type: str) -> str:
    pdf_bytes = render_invoice_pdf_bytes(
        InvoicePDFData(
            number="2026-0042",
            status="paid",
            language="cs",
            invoice_style="modern",
            document_type=document_type,
            document_label="Dobropis" if document_type == "credit_note" else "Faktura",
            issue_date=date(2026, 3, 10),
            taxable_supply_date=date(2026, 3, 10),
            due_date=date(2026, 3, 24),
            currency="CZK",
            items_total_cents=12_000,
            discount_cents=0,
            rounding_adjustment_cents=0,
            total_cents=-12_000 if document_type == "credit_note" else 12_000,
            notes=None,
            issuer={"name": "Test subject"},
            customer={"name": "Acme Client a.s."},
            items=[
                {
                    "description": "Správa infrastruktury",
                    "quantity": "2",
                    "unit": "hod",
                    "unit_price_cents": -6_000 if document_type == "credit_note" else 6_000,
                    "vat_rate": "0",
                    "line_total_cents": -12_000 if document_type == "credit_note" else 12_000,
                }
            ],
        )
    )
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_reportlab_paid_notice_is_only_rendered_for_non_credit_documents():
    assert "Tento doklad je již uhrazený" in _paid_document_pdf_text(document_type="invoice")
    assert "Tento doklad je již uhrazený" not in _paid_document_pdf_text(document_type="credit_note")

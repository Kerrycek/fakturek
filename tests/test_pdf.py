from fastapi.testclient import TestClient

from fakturek.main import create_app


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

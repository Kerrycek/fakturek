import pytest


def test_render_html_pdf_bytes_returns_pdf_magic_header():
    """WeasyPrint HTML->PDF helper should return bytes starting with %PDF.

    The project treats WeasyPrint as an optional dependency at runtime.
    If it's not installed, we skip the test.
    """

    from fakturek.pdf import render_html_pdf_bytes

    try:
        pdf = render_html_pdf_bytes("<h1>Hello</h1><p>PDF test</p>")
    except RuntimeError:
        pytest.skip("weasyprint is not available")

    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf).startswith(b"%PDF")
    assert len(pdf) > 1000

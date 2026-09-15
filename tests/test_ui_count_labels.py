from __future__ import annotations

import pytest

from fakturek.ui_i18n import format_ui_count, translate_html_document


@pytest.mark.parametrize(
    ("entity", "forms"),
    [
        ("document", ("dokladů", "doklad", "doklady", "doklady", "dokladů")),
        ("invoice", ("faktur", "faktura", "faktury", "faktury", "faktur")),
        ("contact", ("kontaktů", "kontakt", "kontakty", "kontakty", "kontaktů")),
    ],
)
def test_format_ui_count_uses_czech_forms(entity: str, forms: tuple[str, ...]) -> None:
    for count, expected_form in zip((0, 1, 2, 4, 5), forms, strict=True):
        assert format_ui_count(count, entity, "cs") == f"{count} {expected_form}"


@pytest.mark.parametrize(
    ("entity", "singular", "plural"),
    [
        ("document", "document", "documents"),
        ("invoice", "invoice", "invoices"),
        ("contact", "contact", "contacts"),
    ],
)
def test_format_ui_count_uses_english_singular_and_plural(
    entity: str,
    singular: str,
    plural: str,
) -> None:
    for count in (0, 1, 2, 4, 5):
        expected_form = singular if count == 1 else plural
        assert format_ui_count(count, entity, "en") == f"{count} {expected_form}"


def test_format_ui_count_inflects_paid_invoice_summary() -> None:
    assert format_ui_count(0, "paid_invoice", "cs") == "0 uhrazeno"
    assert format_ui_count(1, "paid_invoice", "cs") == "1 uhrazena"
    assert format_ui_count(2, "paid_invoice", "cs") == "2 uhrazeny"
    assert format_ui_count(4, "paid_invoice", "cs") == "4 uhrazeny"
    assert format_ui_count(5, "paid_invoice", "cs") == "5 uhrazeno"
    assert format_ui_count(1, "paid_invoice", "en") == "1 paid"
    assert format_ui_count(5, "paid_invoice", "en") == "5 paid"


def test_html_translation_accepts_inflected_czech_count_phrases() -> None:
    html = (
        "<p>1 faktura · 1 uhrazena</p>"
        "<p>2 faktury · 2 uhrazeny</p>"
        "<p>5 faktur · 5 uhrazeno</p>"
        "<p>Mimo hlavní měnu v tomto roce: 2 faktury.</p>"
    )

    translated = translate_html_document(html, "en")

    assert "1 invoice · 1 paid" in translated
    assert "2 invoices · 2 paid" in translated
    assert "5 invoices · 5 paid" in translated
    assert "Outside the main currency this year: 2 invoices." in translated

from __future__ import annotations

INVOICE_PDF_THEME_OPTIONS: list[tuple[str, str]] = [
    ("standard", "Standard"),
    ("classic", "Klasický"),
    ("minimal", "Minimal"),
]

INVOICE_PDF_THEME_DESCRIPTIONS: dict[str, str] = {
    "standard": "Čistý výchozí vzhled pro běžné faktury.",
    "classic": "Konzervativnější papírový styl pro účetní a instituce.",
    "minimal": "Úsporná černobílá varianta s minimem barev.",
}

VALID_INVOICE_PDF_THEMES = {value for value, _label in INVOICE_PDF_THEME_OPTIONS}


def normalize_invoice_pdf_theme(value: str | None) -> str:
    normalized = str(value or "standard").strip().lower() or "standard"
    return normalized if normalized in VALID_INVOICE_PDF_THEMES else "standard"


def pdf_theme_to_invoice_style(value: str | None) -> str:
    normalized = normalize_invoice_pdf_theme(value)
    if normalized == "standard":
        return "modern"
    return normalized

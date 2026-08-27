from __future__ import annotations

"""PDF rendering helpers.

This project supports two PDF strategies:

1) **HTML → PDF preview** via WeasyPrint (phase-19)
   - Renders the same template as the "print" page.
   - Usually produces the nicest output.

2) **Fallback PDF** via ReportLab (introduced earlier)
   - Pure-Python and very reliable.
   - Used as a fallback when WeasyPrint is unavailable or fails.
   - Also used for small "error PDFs" when DB is down.

The goal is not typographic perfection, but predictable and debuggable output.
"""

from dataclasses import dataclass, field
import base64
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from fakturek.money import format_cents, format_quantity
from fakturek.banking import format_iban_for_display


# --- Optional dependency: WeasyPrint -----------------------------------
#
# WeasyPrint occasionally ends up with an incompatible `pydyf` version
# (eg. system packages vs. pip). We patch a couple of renamed methods
# at runtime to keep PDF generation working.
try:  # pragma: no cover
    from weasyprint import HTML as _WeasyHTML

    _WEASYPRINT_AVAILABLE = True
except Exception:  # pragma: no cover
    # Treat any import failure as "not available".
    # In minimal environments WeasyPrint can be present but broken due to
    # missing native libraries (pango/cairo/etc.). We keep the app importable
    # and let callers fall back to the ReportLab renderer.
    _WEASYPRINT_AVAILABLE = False


# --- Optional dependency -------------------------------------------------
#
# Keep the app importable even if reportlab is missing.
try:  # pragma: no cover
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Image,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    _REPORTLAB_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    _REPORTLAB_AVAILABLE = False


def _patch_weasyprint_pydyf_compat() -> None:
    """Patch pydyf API for older WeasyPrint versions.

    WeasyPrint 53.x expects `Stream.transform` and `Stream.text_matrix`, while
    newer pydyf versions renamed these to `set_matrix` and `set_text_matrix`.
    The patch is safe and idempotent.
    """

    try:  # pragma: no cover
        import pydyf
    except Exception:  # pragma: no cover
        return

    if not hasattr(pydyf.Stream, "transform") and hasattr(pydyf.Stream, "set_matrix"):

        def _transform(self, a=1, b=0, c=0, d=1, e=0, f=0):  # type: ignore[no-redef]
            return self.set_matrix(a, b, c, d, e, f)

        pydyf.Stream.transform = _transform  # type: ignore[attr-defined]

    if not hasattr(pydyf.Stream, "text_matrix") and hasattr(pydyf.Stream, "set_text_matrix"):

        def _text_matrix(self, a, b, c, d, e, f):  # type: ignore[no-redef]
            return self.set_text_matrix(a, b, c, d, e, f)

        pydyf.Stream.text_matrix = _text_matrix  # type: ignore[attr-defined]


@dataclass(frozen=True)
class InvoicePDFData:
    number: str
    status: str
    issue_date: date
    taxable_supply_date: date | None
    due_date: date
    currency: str
    items_total_cents: int
    discount_cents: int
    rounding_adjustment_cents: int
    total_cents: int
    notes: str | None

    issuer: dict[str, str]
    customer: dict[str, str]
    items: list[dict[str, Any]]
    language: str = "cs"
    invoice_style: str = "modern"
    document_type: str = "invoice"
    document_label: str = "Faktura"
    payment_method: str = "bank_transfer"
    variable_symbol: str = ""
    footer_text: str | None = None
    source_invoice_number: str | None = None
    payment_account: dict[str, str] = field(default_factory=dict)
    payment_qr_codes: list[dict[str, str]] = field(default_factory=list)


def _register_dejavu_fonts() -> None:
    """Register DejaVu fonts (unicode-capable) if available."""

    if not _REPORTLAB_AVAILABLE:  # pragma: no cover
        return

    # Idempotent.
    if "DejaVuSans" in pdfmetrics.getRegisteredFontNames():
        return

    font_pairs = [
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ),
        (
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ),
        (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ),
        (
            Path(reportlab.__file__).resolve().parent / "fonts" / "Vera.ttf",
            Path(reportlab.__file__).resolve().parent / "fonts" / "VeraBd.ttf",
        ),
    ]

    regular, bold = next(((regular, bold) for regular, bold in font_pairs if regular.exists()), (None, None))

    if regular is None:
        return

    pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
    if bold is not None and bold.exists():
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return xml_escape(str(value))


def _status_label(status: str | None) -> str:
    s = (status or "").strip().lower()
    return {
        "draft": "draft",
        "issued": "vystavená",
        "sent": "odeslaná",
        "paid": "zaplacená",
    }.get(s, status or "")


def _safe_filename(value: str, *, fallback: str = "invoice") -> str:
    """Return a filesystem-safe filename base (no extension)."""

    v = (value or "").strip()
    if not v:
        return fallback

    keep = []
    for ch in v:
        if ch.isalnum() or ch in {"-", "_"}:
            keep.append(ch)
        else:
            keep.append("-")

    out = "".join(keep).strip("-")
    return out or fallback


def _decode_png_data_uri(data_uri: str | None) -> BytesIO | None:
    raw = str(data_uri or "").strip()
    if not raw.startswith("data:image/png;base64,"):
        return None
    try:
        payload = raw.split(",", 1)[1]
        return BytesIO(base64.b64decode(payload))
    except Exception:
        return None


def render_invoice_pdf_bytes(data: InvoicePDFData) -> bytes:
    """Render invoice PDF and return bytes."""

    if not _REPORTLAB_AVAILABLE:
        raise RuntimeError("PDF export unavailable (missing dependency: reportlab).")

    _register_dejavu_fonts()

    buf = BytesIO()

    style_key = str(getattr(data, "invoice_style", "modern") or "modern").strip().lower()
    palettes = {
        "modern": {
            "accent": "#1f7a38",
            "header_bg": "#f5f8f7",
            "header_total_bg": "#eef3f0",
            "header_border": "#d7e3dc",
            "box_bg": "#f8fafc",
            "box_border": "#d9e2ea",
            "muted": "#5f6b7a",
            "text": "#111827",
        },
        "classic": {
            "accent": "#315f8b",
            "header_bg": "#f4efe7",
            "header_total_bg": "#eef4fa",
            "header_border": "#d9d1c5",
            "box_bg": "#fffefb",
            "box_border": "#d9d1c5",
            "muted": "#6f675c",
            "text": "#211f1a",
        },
        "minimal": {
            "accent": "#111111",
            "header_bg": "#f3f4f6",
            "header_total_bg": "#f3f4f6",
            "header_border": "#d1d5db",
            "box_bg": "#ffffff",
            "box_border": "#e5e7eb",
            "muted": "#6b7280",
            "text": "#111111",
        },
    }
    if style_key == "standard":
        style_key = "modern"
    palette = palettes.get(style_key, palettes["modern"])

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"{data.document_label} {data.number}",
    )

    styles = getSampleStyleSheet()

    base_font = "DejaVuSans" if "DejaVuSans" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    bold_font = "DejaVuSans-Bold" if "DejaVuSans-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"

    title_style = ParagraphStyle(
        "title",
        parent=styles["Heading1"],
        fontName=bold_font,
        fontSize=21,
        leading=24,
        textColor=colors.HexColor(palette["accent"]),
        spaceAfter=4,
    )
    header_value_style = ParagraphStyle(
        "header_value",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=10,
        leading=13,
        textColor=colors.HexColor(palette["text"]),
    )
    card_title_style = ParagraphStyle(
        "card_title",
        parent=styles["Heading3"],
        fontName=bold_font,
        fontSize=11,
        leading=13,
        textColor=colors.HexColor(palette["text"]),
        spaceAfter=2,
    )
    p_style = ParagraphStyle(
        "p",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=9.6,
        leading=12.2,
        textColor=colors.HexColor("#111827"),
    )
    small_style = ParagraphStyle(
        "small",
        parent=p_style,
        fontSize=8.6,
        leading=11,
        textColor=colors.HexColor(palette["muted"]),
    )
    table_header_style = ParagraphStyle(
        "table_header",
        parent=small_style,
        fontName=bold_font,
        textColor=colors.HexColor("#111827"),
        alignment=1,
    )
    table_cell_style = ParagraphStyle(
        "table_cell",
        parent=p_style,
        alignment=0,
    )
    table_num_style = ParagraphStyle(
        "table_num",
        parent=p_style,
        alignment=2,
    )
    total_label_style = ParagraphStyle(
        "total_label",
        parent=p_style,
        fontName=bold_font,
        alignment=0,
    )
    total_number_style = ParagraphStyle(
        "total_number",
        parent=p_style,
        fontName=bold_font,
        alignment=2,
    )
    note_style = ParagraphStyle(
        "note",
        parent=p_style,
        borderPadding=8,
        leading=13,
    )
    paid_notice_style = ParagraphStyle(
        "paid_notice",
        parent=p_style,
        fontName=bold_font,
        fontSize=8.8,
        leading=11,
        textColor=colors.HexColor("#166534"),
    )
    footer_style = ParagraphStyle(
        "footer",
        parent=small_style,
        alignment=1,
    )

    def _norm_text(value: Any) -> str:
        if value is None:
            return ""
        return xml_escape(str(value))

    def _status_label(status: str | None) -> str:
        s = (status or "").strip().lower()
        return {
            "draft": "draft",
            "issued": "vystavená",
            "sent": "odeslaná",
            "paid": "zaplacená",
        }.get(s, status or "")

    def _addr_block(d: dict[str, str], *, include_email: bool = False) -> str:
        lines: list[str] = []
        name = _norm_text(d.get("name"))
        if name:
            lines.append(f"<b>{name}</b>")

        street = _norm_text(d.get("street"))
        city = _norm_text(d.get("city"))
        zip_ = _norm_text(d.get("zip"))
        country = _norm_text(d.get("country"))
        if street:
            lines.append(street)
        line2 = " ".join([p for p in [zip_, city] if p]).strip()
        if line2:
            lines.append(line2)
        if country:
            lines.append(country)

        ico = _norm_text(d.get("ico"))
        dic = _norm_text(d.get("dic"))
        email = _norm_text(d.get("email"))
        phone = _norm_text(d.get("phone"))
        bank = _norm_text(d.get("bank_account"))

        if ico:
            lines.append(f"IČO: {ico}")
        if dic:
            lines.append(f"DIČ: {dic}")
        if email and include_email:
            lines.append(f"Email: {email}")
        if phone:
            lines.append(f"Telefon: {phone}")
        if bank:
            lines.append(f"Účet: {bank}")

        if not lines:
            lines.append("—")
        return "<br/>".join(lines)

    story: list[Any] = []

    payment_method_label = {
        "bank_transfer": "Převodem",
        "cash": "Hotově",
        "card": "Kartou",
        "cod": "Dobírkou",
    }.get(str(data.payment_method or "").strip().lower(), _norm_text(data.payment_method))

    story.append(
        Paragraph(
            f"{_norm_text(data.document_label)} {_norm_text(data.number)}",
            title_style,
        )
    )
    if _norm_text(data.source_invoice_number):
        story.append(Paragraph(f"Navázaný doklad: {_norm_text(data.source_invoice_number)}", header_value_style))
    story.append(Spacer(1, 4 * mm))

    header_cells = [
        Paragraph(f"<b>Vystaveno</b><br/>{_norm_text(data.issue_date)}", header_value_style),
        Paragraph(f"<b>Splatnost</b><br/>{_norm_text(data.due_date)}", header_value_style),
    ]
    header_widths = [42 * mm, 42 * mm]
    if data.taxable_supply_date is not None:
        header_cells.insert(1, Paragraph(f"<b>DUZP</b><br/>{_norm_text(data.taxable_supply_date)}", header_value_style))
        header_widths = [40 * mm, 40 * mm, 40 * mm]

    header_cards = Table([header_cells], colWidths=header_widths, hAlign="LEFT")
    header_cards.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette["header_bg"])),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor(palette["header_border"])),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor(palette["header_border"])),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(header_cards)
    story.append(Spacer(1, 6 * mm))

    issuer_address = dict(data.issuer or {})
    if str(data.payment_method or "").strip().lower() != "bank_transfer":
        issuer_address["bank_account"] = ""

    parties_table = Table(
        [
            [Paragraph("Vystavovatel", card_title_style), Paragraph("Odběratel", card_title_style)],
            [Paragraph(_addr_block(issuer_address, include_email=True), p_style), Paragraph(_addr_block(data.customer), p_style)],
        ],
        colWidths=[93 * mm, 93 * mm],
        hAlign="LEFT",
    )
    parties_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette["box_bg"])),
                ("BACKGROUND", (0, 1), (-1, 1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(palette["box_border"])),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor(palette["box_border"])),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(parties_table)
    story.append(Spacer(1, 5 * mm))

    payment_lines: list[str] = []
    payment = dict(data.payment_account or {})
    if payment_method_label:
        payment_lines.append(f"Způsob platby: {payment_method_label}")
    if str(data.payment_method or "").strip().lower() == "bank_transfer":
        if payment:
            display = _norm_text(payment.get("display")) or _norm_text(payment.get("number"))
            iban_raw = str(payment.get("iban") or "")
            iban = _norm_text(payment.get("iban_display") or format_iban_for_display(iban_raw) or iban_raw)
            bic = _norm_text(payment.get("bic"))
            country = _norm_text(payment.get("country")).upper()
            if display and country != "SK":
                payment_lines.append(f"Číslo účtu: {display}")
            if _norm_text(data.variable_symbol):
                payment_lines.append(f"Variabilní symbol: {_norm_text(data.variable_symbol)}")
            if bic:
                payment_lines.append(f"BIC / SWIFT: {bic}")
            if iban:
                payment_lines.append(f"IBAN: {iban}")
        elif _norm_text(data.issuer.get("bank_account")):
            payment_lines.append(f"Číslo účtu: {_norm_text(data.issuer.get('bank_account'))}")

    qr_box = None
    if str(data.payment_method or "").strip().lower() == "bank_transfer":
        for qr in list(data.payment_qr_codes or []):
            png = _decode_png_data_uri(qr.get("image_data_uri") if isinstance(qr, dict) else None)
            if png is None:
                continue
            img = Image(png, width=22 * mm, height=22 * mm)
            img.hAlign = "CENTER"
            qr_box = Table(
                [[img], [Paragraph(f"<b>{_norm_text(qr.get('title') if isinstance(qr, dict) else 'QR platba')}</b>", small_style)]],
                colWidths=[28 * mm],
                hAlign="CENTER",
            )
            qr_box.setStyle(
                TableStyle(
                    [
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(palette["box_border"])),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            break

    if payment_lines:
        if qr_box is not None:
            payment_table = Table(
                [
                    [Paragraph("Platba", card_title_style), ""],
                    [Paragraph("<br/>".join(payment_lines), p_style), qr_box],
                ],
                colWidths=[140 * mm, 38 * mm],
                hAlign="LEFT",
            )
            payment_styles = [
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette["box_bg"])),
                ("BACKGROUND", (0, 1), (-1, 1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(palette["box_border"])),
                ("VALIGN", (0, 1), (-1, 1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        else:
            payment_table = Table(
                [[Paragraph("Platba", card_title_style)], [Paragraph("<br/>".join(payment_lines), p_style)]],
                colWidths=[178 * mm],
                hAlign="LEFT",
            )
            payment_styles = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette["box_bg"])),
                ("BACKGROUND", (0, 1), (-1, 1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(palette["box_border"])),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        payment_table.setStyle(TableStyle(payment_styles))
        story.append(payment_table)
        story.append(Spacer(1, 5 * mm))

    story.append(Paragraph("Položky", card_title_style))

    show_vat = any(str((it.get("vat_rate") if isinstance(it, dict) else "") or "").strip() not in {"", "0", "0.0", "0.00"} for it in data.items)
    table_header = [Paragraph("Popis", table_header_style), Paragraph("Množství", table_header_style), Paragraph("Jedn. cena", table_header_style)]
    widths = [98 * mm, 26 * mm, 34 * mm]
    if show_vat:
        widths = [78 * mm, 24 * mm, 34 * mm]
        table_header.append(Paragraph("DPH", table_header_style))
        widths.append(20 * mm)
    table_header.append(Paragraph("Celkem", table_header_style))
    widths.append(28 * mm if show_vat else 28 * mm)

    table_data: list[list[Any]] = [table_header]
    for it in data.items:
        desc = _norm_text(it.get("description") if isinstance(it, dict) else "")
        qty = format_quantity(it.get("quantity") if isinstance(it, dict) else "")
        item_unit = _norm_text(it.get("unit") if isinstance(it, dict) else "")
        unit = format_cents(int((it.get("unit_price_cents") if isinstance(it, dict) else 0) or 0), data.currency)
        vat = _norm_text(it.get("vat_rate") if isinstance(it, dict) else "")
        line_total = format_cents(int((it.get("line_total_cents") if isinstance(it, dict) else 0) or 0), data.currency)
        row: list[Any] = [
            Paragraph(desc or "—", table_cell_style),
            Paragraph(" ".join(part for part in [qty, item_unit] if part).strip(), table_num_style),
            Paragraph(unit, table_num_style),
        ]
        if show_vat:
            row.append(Paragraph(f"{vat}%" if vat else "", table_num_style))
        row.append(Paragraph(line_total, table_num_style))
        table_data.append(row)

    items_table = Table(table_data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3f0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#d9e2ea")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3e8ee")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    totals_rows: list[list[Any]] = [
        [Paragraph("Mezisoučet", p_style), Paragraph(format_cents(int(data.items_total_cents or 0), data.currency), table_num_style)]
    ]
    if int(data.discount_cents or 0):
        totals_rows.append([Paragraph("Sleva", p_style), Paragraph(format_cents(-int(data.discount_cents or 0), data.currency), table_num_style)])
    if int(data.rounding_adjustment_cents or 0):
        totals_rows.append([Paragraph("Zaokrouhlení", p_style), Paragraph(format_cents(int(data.rounding_adjustment_cents or 0), data.currency), table_num_style)])
    totals_rows.append([
        Paragraph("<b>Celkem</b>", total_label_style),
        Paragraph(format_cents(int(data.total_cents or 0), data.currency), total_number_style),
    ])

    totals_table = Table(totals_rows, colWidths=[42 * mm, 34 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -2), colors.white),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef3f0")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#d9e2ea")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3e8ee")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(totals_table)

    if data.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("Poznámka", card_title_style))
        note_table = Table([[Paragraph(_norm_text(data.notes), note_style)]], colWidths=[178 * mm], hAlign="LEFT")
        note_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                    ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#d9e2ea")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(note_table)

    if (
        str(data.status or "").strip().lower() == "paid"
        and str(data.document_type or "invoice").strip().lower() != "credit_note"
    ):
        paid_notice = (
            "This document has already been paid. Please do not pay it again."
            if str(data.language or "cs").strip().lower().startswith("en")
            else "Tento doklad je již uhrazený. Neplaťte jej prosím znovu."
        )
        story.append(Spacer(1, 4 * mm))
        paid_notice_table = Table(
            [[Paragraph(_norm_text(paid_notice), paid_notice_style)]],
            colWidths=[178 * mm],
            hAlign="LEFT",
        )
        paid_notice_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf3")),
                    ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#86efac")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(paid_notice_table)

    if data.footer_text:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(_norm_text(data.footer_text), footer_style))

    doc.build(story)
    return buf.getvalue()


def render_html_pdf_bytes(
    html: str,
    *,
    base_url: str | Path | None = None,
) -> bytes:
    """Render HTML to PDF using WeasyPrint.

    This is primarily used for preview PDFs that should match the HTML print
    layout.
    """

    if not _WEASYPRINT_AVAILABLE:
        raise RuntimeError("PDF preview unavailable (missing dependency: weasyprint).")

    _patch_weasyprint_pydyf_compat()

    base_url_str: str | None
    if base_url is None:
        base_url_str = None
    else:
        base_url_str = str(base_url)

    try:
        return _WeasyHTML(string=html, base_url=base_url_str).write_pdf()
    except Exception as exc:
        raise RuntimeError("PDF preview unavailable (weasyprint failed).") from exc



def render_error_pdf_bytes(
    *,
    title: str,
    message: str,
    request_path: str | None = None,
) -> bytes:
    """Render a small PDF with an error message."""

    if not _REPORTLAB_AVAILABLE:
        raise RuntimeError("PDF export unavailable (missing dependency: reportlab).")

    _register_dejavu_fonts()

    base_font = "DejaVuSans" if "DejaVuSans" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    bold_font = "DejaVuSans-Bold" if "DejaVuSans-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title",
        parent=styles["Heading1"],
        fontName=bold_font,
        fontSize=18,
        leading=22,
        spaceAfter=10,
    )
    p_style = ParagraphStyle(
        "p",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=10,
        leading=13,
    )

    story: list[Any] = [
        Paragraph(_norm_text(title), title_style),
        Paragraph(_norm_text(message), p_style),
    ]
    if request_path:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"Cesta: {_norm_text(request_path)}", p_style))

    doc.build(story)
    return buf.getvalue()


def content_disposition_inline(invoice_number: str | None, *, suffix: str = ".pdf") -> str:
    name = _safe_filename(invoice_number or "", fallback="invoice")
    return f'inline; filename="{name}{suffix}"'


def content_disposition_attachment(invoice_number: str | None, *, suffix: str = ".pdf") -> str:
    name = _safe_filename(invoice_number or "", fallback="invoice")
    return f'attachment; filename="{name}{suffix}"'

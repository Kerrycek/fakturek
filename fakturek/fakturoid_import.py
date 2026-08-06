from __future__ import annotations

"""Fakturoid import helpers (phase-25/26).

Phase-24 introduced upload infra (ImportRun + file storage).
Phase-25 added tolerant parsing + importing of invoices from Fakturoid XML (or ZIP with XML).
Phase-26 extends the importer with:

- contacts import from Fakturoid CSV (or ZIP with CSV)
- best-effort reconciliation of invoice numbering series based on existing invoice numbers

Design goals:

* Be tolerant to slightly different export shapes (namespaces, dashed tags,
  Rails-style type attributes, single-invoice vs multi-invoice exports).
* Keep DB writes idempotent using ImportMap and invoice-number uniqueness.
* Prefer exact monetary values from export (line totals, invoice total) and
  use rounding_adjustment_cents when needed.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import csv
import io
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from fakturek.banking import digits_only, normalize_iban, normalize_spaces, resolve_bank_account
from fakturek.security import ensure_safe_xml_bytes
from fakturek.money import (
    compute_line_amounts_cents,
    parse_money_to_cents,
    parse_quantity,
    parse_vat_rate,
)

try:  # pragma: no cover - optional dependency in some environments
    from pypdf import PdfReader

    _PYPDF_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    PdfReader = None  # type: ignore[assignment]
    _PYPDF_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _safe_xml_fromstring(xml_bytes: bytes) -> ET.Element:
    """Parse untrusted XML with defusedxml after size/entity preflight."""
    safe_bytes = ensure_safe_xml_bytes(xml_bytes)
    try:
        return SafeET.fromstring(safe_bytes)
    except DefusedXmlException as exc:
        raise ValueError("XML obsahuje nepovolené deklarace") from exc


def _strip_ns(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _norm(tag: str) -> str:
    return _strip_ns(tag).strip().lower().replace("-", "_")


def _txt(el: ET.Element | None) -> str:
    if el is None:
        return ""
    t = el.text
    return (t or "").strip()


def _child_text_map(el: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for ch in list(el):
        k = _norm(ch.tag)
        # Keep the first occurrence (stable) for simple scalar fields.
        if k not in out:
            out[k] = _txt(ch)
    return out


def _first_child(el: ET.Element | None, *names: str) -> ET.Element | None:
    if el is None:
        return None
    wanted = {_norm(name) for name in names if str(name or "").strip()}
    for ch in list(el):
        if _norm(ch.tag) in wanted:
            return ch
    return None


def _iter_descendants(el: ET.Element | None, *names: str) -> list[ET.Element]:
    if el is None:
        return []
    wanted = {_norm(name) for name in names if str(name or "").strip()}
    return [node for node in el.iter() if node is not el and _norm(node.tag) in wanted]


def _first_descendant(el: ET.Element | None, *names: str) -> ET.Element | None:
    matches = _iter_descendants(el, *names)
    return matches[0] if matches else None


def _parse_date(value: str) -> date | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except Exception:
        return None


def _parse_datetime(value: str) -> datetime | None:
    v = (value or "").strip()
    if not v:
        return None
    # Handle trailing Z.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except Exception:
        return None
    # Convert aware -> naive UTC for storage (DB columns are naive DateTime).
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _map_import_payment_method(value: str | None) -> str:
    raw = (str(value or "") or "").strip().lower()
    if raw in {"bank", "bank_transfer", "transfer", "wire", "wire_transfer"}:
        return "bank_transfer"
    if raw in {"cash", "hotove", "hotově"}:
        return "cash"
    if raw in {"card", "kartou", "karta"}:
        return "card"
    if raw in {"cod", "dobirka", "dobírka"}:
        return "cod"
    return "bank_transfer"


def _default_invoice_footer_mode_for_subject(subject) -> str:
    stored_mode = (getattr(subject, "default_invoice_footer_mode", None) or "").strip().lower()
    if stored_mode in {"trade_register", "commercial_register", "association_register", "custom"}:
        return stored_mode
    name = normalize_spaces(str(getattr(subject, "name", "") or "")).lower()
    if "z.s." in name or " spolek" in name or name.startswith("spolek "):
        return "association_register"
    if any(token in name for token in ("s.r.o.", "a.s.", "zapsan", "společnost", "firma")):
        return "commercial_register"
    return "trade_register"


def _invoice_footer_text_for_mode(mode: str | None, *, subject) -> str:
    normalized_mode = (mode or "").strip().lower() or _default_invoice_footer_mode_for_subject(subject)
    if normalized_mode == "custom":
        return str(getattr(subject, "default_invoice_footer_text", None) or "")
    if normalized_mode == "association_register":
        return "Spolek zapsaný ve spolkovém rejstříku."
    if normalized_mode == "commercial_register":
        return "Společnost zapsaná v obchodním rejstříku."
    return "Fyzická osoba zapsaná v živnostenském rejstříku."


def _looks_like_zip(data: bytes) -> bool:
    return len(data) >= 4 and data[:2] == b"PK"


def _looks_like_xml(data: bytes) -> bool:
    sniff = data.lstrip()[:20]
    return sniff.startswith(b"<") or b"<?xml" in data[:100]


def _looks_like_pdf(data: bytes) -> bool:
    return data.lstrip()[:5] == b"%PDF-"


def _safe_zip_member_name(name: str) -> bool:
    """Reject suspicious ZIP member names (path traversal)."""

    n = (name or "").replace("\\", "/")
    if not n:
        return False
    if n.startswith("/") or n.startswith("..") or "/../" in n:
        return False
    return True


def _extract_first_file_from_zip_bytes(
    data: bytes,
    *,
    preferred_exts: tuple[str, ...],
    max_uncompressed_bytes: int = 50 * 1024 * 1024,
    max_total_uncompressed_bytes: int = 100 * 1024 * 1024,
    max_members: int = 250,
) -> tuple[str, bytes]:
    """Return (name, bytes) for the first reasonable file in a ZIP.

    Selection rules:

    - Prefer files with extensions in ``preferred_exts`` (case-insensitive).
    - Fall back to any file if none match.
    - Deterministic order.
    """

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > int(max_members):
            raise ValueError("ZIP obsahuje příliš mnoho souborů")
        total_uncompressed = sum(max(0, int(getattr(i, "file_size", 0) or 0)) for i in infos)
        if total_uncompressed > int(max_total_uncompressed_bytes):
            raise ValueError("ZIP je příliš velký po rozbalení")
        preferred: list[zipfile.ZipInfo] = []
        for i in infos:
            fn = (i.filename or "").lower()
            if any(fn.endswith(ext) for ext in preferred_exts):
                preferred.append(i)
        candidates = preferred or infos
        # Deterministic order.
        candidates.sort(key=lambda i: ("/" in (i.filename or ""), (i.filename or "").lower()))

        for info in candidates:
            if info.file_size <= 0:
                continue
            if info.file_size > max_uncompressed_bytes:
                continue
            name = info.filename
            if not _safe_zip_member_name(name):
                continue
            with zf.open(info) as fp:
                content = fp.read(max_uncompressed_bytes + 1)
            if len(content) > max_uncompressed_bytes:
                continue
            return name, content

    raise ValueError("ZIP neobsahuje žádný použitelný soubor")


def _extract_xml_from_zip_bytes(
    data: bytes,
    *,
    max_uncompressed_bytes: int = 50 * 1024 * 1024,
) -> tuple[str, bytes]:
    """Return (name, xml_bytes) for the first reasonable *.xml file in a zip."""

    name, content = _extract_first_file_from_zip_bytes(
        data,
        preferred_exts=(".isdoc", ".xml"),
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    # Heuristic: likely XML starts with < after BOM/whitespace.
    if not _looks_like_xml(content):
        raise ValueError("ZIP neobsahuje žádný použitelný XML soubor")
    return name, ensure_safe_xml_bytes(content, max_bytes=max_uncompressed_bytes)


def _extract_csv_from_zip_bytes(
    data: bytes,
    *,
    max_uncompressed_bytes: int = 50 * 1024 * 1024,
) -> tuple[str, bytes]:
    """Return (name, csv_bytes) for the first reasonable *.csv file in a zip."""

    name, content = _extract_first_file_from_zip_bytes(
        data,
        preferred_exts=(".csv",),
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    # Simple sanity: must contain at least one newline and some delimiter.
    sniff = content[:4096]
    if b"\n" not in sniff and b"\r" not in sniff:
        raise ValueError("ZIP neobsahuje žádný použitelný CSV soubor")
    return name, content


# ---------------------------------------------------------------------------
# Payload normalization (backwards compatible helpers)
# ---------------------------------------------------------------------------


def _payload_to_xml_bytes(filename: str, payload: bytes) -> tuple[str, bytes]:
    """Normalize payload into XML bytes (accepts raw XML or ZIP with XML)."""

    name = filename or "import"
    if _looks_like_zip(payload) or name.lower().endswith(".zip"):
        inner_name, xml_bytes = _extract_xml_from_zip_bytes(payload)
        return inner_name, xml_bytes
    # Treat everything else as XML.
    return name, payload


def _payload_to_csv_bytes(filename: str, payload: bytes) -> tuple[str, bytes]:
    """Normalize payload into CSV bytes (accepts raw CSV or ZIP with CSV)."""

    name = filename or "import"
    if _looks_like_zip(payload) or name.lower().endswith(".zip"):
        inner_name, csv_bytes = _extract_csv_from_zip_bytes(payload)
        return inner_name, csv_bytes
    return name, payload


@dataclass(frozen=True)
class ImportAssets:
    """Detected import assets from one uploaded file."""

    xml_name: str | None = None
    xml_bytes: bytes | None = None

    csv_name: str | None = None
    csv_bytes: bytes | None = None
    pdf_files: list[tuple[str, bytes]] = field(default_factory=list)


def _payload_to_assets(filename: str, payload: bytes) -> ImportAssets:
    """Detect supported assets in the uploaded file.

    Supported shapes:

    - raw XML (invoices)
    - raw CSV (contacts)
    - ZIP that contains XML and/or CSV
    """

    name = filename or "import"

    if _looks_like_zip(payload) or name.lower().endswith(".zip"):
        xml_name = None
        xml_bytes = None
        csv_name = None
        csv_bytes = None
        pdf_files: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > 250:
                raise ValueError("ZIP obsahuje příliš mnoho souborů")
            total_uncompressed = sum(max(0, int(getattr(i, "file_size", 0) or 0)) for i in infos)
            if total_uncompressed > 100 * 1024 * 1024:
                raise ValueError("ZIP je příliš velký po rozbalení")
            # Deterministic order.
            infos.sort(key=lambda i: ("/" in (i.filename or ""), (i.filename or "").lower()))

            def read_member(info: zipfile.ZipInfo, max_bytes: int = 50 * 1024 * 1024) -> bytes:
                if info.file_size <= 0 or info.file_size > max_bytes:
                    return b""
                if not _safe_zip_member_name(info.filename):
                    return b""
                with zf.open(info) as fp:
                    content = fp.read(max_bytes + 1)
                if len(content) > max_bytes:
                    return b""
                return content

            for info in infos:
                fn = (info.filename or "").lower()
                if (fn.endswith(".xml") or fn.endswith(".isdoc")) and xml_bytes is None:
                    content = read_member(info)
                    if content and _looks_like_xml(content):
                        xml_name, xml_bytes = info.filename, ensure_safe_xml_bytes(content)
                if fn.endswith(".csv") and csv_bytes is None:
                    content = read_member(info)
                    if content:
                        csv_name, csv_bytes = info.filename, content
                if fn.endswith(".pdf"):
                    content = read_member(info)
                    if content and _looks_like_pdf(content):
                        pdf_files.append((info.filename, content))
                if xml_bytes is not None and csv_bytes is not None:
                    break

        return ImportAssets(
            xml_name=xml_name,
            xml_bytes=xml_bytes,
            csv_name=csv_name,
            csv_bytes=csv_bytes,
            pdf_files=pdf_files,
        )

    # Non-zip: decide by content/ext.
    lower = name.lower()
    if lower.endswith((".xml", ".isdoc")) or _looks_like_xml(payload):
        return ImportAssets(xml_name=name, xml_bytes=ensure_safe_xml_bytes(payload))
    if lower.endswith(".csv"):
        return ImportAssets(csv_name=name, csv_bytes=payload)
    if lower.endswith(".pdf") or _looks_like_pdf(payload):
        return ImportAssets(pdf_files=[(name, payload)])

    # Heuristic: try CSV if it's clearly not XML.
    sniff = payload[:4096]
    if not _looks_like_xml(sniff) and (b"," in sniff or b";" in sniff) and (b"\n" in sniff or b"\r" in sniff):
        return ImportAssets(csv_name=name, csv_bytes=payload)

    # Default to XML (backwards compatibility).
    return ImportAssets(xml_name=name, xml_bytes=ensure_safe_xml_bytes(payload))


# ---------------------------------------------------------------------------
# Parsed structures
# ---------------------------------------------------------------------------


@dataclass
class ParsedParty:
    name: str = ""
    email: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    zip: str = ""
    country: str = "CZ"
    ico: str = ""
    dic: str = ""


@dataclass
class ParsedLine:
    description: str
    quantity: Decimal
    unit_price_cents: int
    vat_rate: Decimal
    net_cents: int
    vat_cents: int
    total_cents: int
    unit: str = ""


@dataclass
class ParsedInvoice:
    external_id: str
    number: str
    variable_symbol: str
    status: str
    issue_date: date | None
    due_date: date | None
    currency: str
    note: str
    private_note: str
    total_cents: int | None
    buyer_external_id: str | None
    buyer: ParsedParty
    seller: ParsedParty | None
    lines: list[ParsedLine]
    sent_at: datetime | None
    paid_on: date | None
    bank_account: str = ""
    iban: str = ""
    bic: str = ""
    taxable_supply_date: date | None = None
    payment_method: str = "bank_transfer"


@dataclass
class ParsedContact:
    external_id: str | None
    name: str
    email: str
    phone: str
    street: str
    city: str
    zip: str
    country: str
    ico: str
    dic: str
    fixed_variable_symbol: str = ""


def _flatten_text_map(el: ET.Element) -> dict[str, str]:
    """Collect scalar text values from an element subtree.

    We keep the first occurrence of each normalized tag to stay deterministic.
    This helps with contact exports where address fields may be nested.
    """

    out: dict[str, str] = {}
    for node in el.iter():
        if node is el:
            continue
        key = _norm(node.tag)
        value = _txt(node)
        if key and value and key not in out:
            out[key] = value
    return out


def _parsed_contact_from_field_map(fields: dict[str, str]) -> ParsedContact:
    external_id = (
        fields.get("id")
        or fields.get("subject_id")
        or fields.get("client_id")
        or fields.get("contact_id")
        or ""
    ).strip() or None

    name = (
        fields.get("name")
        or fields.get("company")
        or fields.get("company_name")
        or fields.get("subject_name")
        or fields.get("client_name")
        or fields.get("full_name")
        or ""
    ).strip()

    if not name:
        first = fields.get("first_name") or fields.get("firstname") or ""
        last = fields.get("last_name") or fields.get("lastname") or ""
        name = f"{first} {last}".strip()

    if not name:
        name = "Bez názvu"

    email = (fields.get("email") or fields.get("mail") or "").strip()
    phone = (fields.get("phone") or fields.get("telephone") or fields.get("mobile") or "").strip()

    street = (
        fields.get("street")
        or fields.get("address")
        or fields.get("address_street")
        or fields.get("line_1")
        or fields.get("line1")
        or ""
    ).strip()
    city = (fields.get("city") or fields.get("town") or "").strip()
    zip_code = (
        fields.get("zip")
        or fields.get("zipcode")
        or fields.get("postal_code")
        or fields.get("psc")
        or ""
    ).strip()
    country = ((fields.get("country") or "CZ").strip().upper() or "CZ")[:2]

    ico = (
        fields.get("registration_no")
        or fields.get("ico")
        or fields.get("company_registration_no")
        or fields.get("ico_dph")
        or ""
    ).strip()
    dic = (fields.get("vat_no") or fields.get("dic") or fields.get("vat_number") or "").strip()
    fixed_variable_symbol = digits_only(
        fields.get("variable_symbol")
        or fields.get("default_variable_symbol")
        or fields.get("payment_reference")
        or ""
    )[:10]

    return ParsedContact(
        external_id=external_id,
        name=name,
        email=email,
        phone=phone,
        street=street,
        city=city,
        zip=zip_code,
        country=country,
        ico=ico,
        dic=dic,
        fixed_variable_symbol=fixed_variable_symbol,
    )


# ---------------------------------------------------------------------------
# Invoice XML parsing
# ---------------------------------------------------------------------------


def _isdoc_direct_text(el: ET.Element | None, name: str) -> str:
    child = _first_child(el, name) if el is not None else None
    return _txt(child)


def _isdoc_party(wrapper: ET.Element | None) -> ParsedParty:
    party = _first_child(wrapper, "Party") or wrapper
    name = _isdoc_direct_text(_first_child(party, "PartyName"), "Name")
    ident = _first_child(party, "PartyIdentification")
    address = _first_child(party, "PostalAddress")
    country_el = _first_child(address, "Country")
    tax_scheme = _first_child(party, "PartyTaxScheme")
    contact = _first_child(party, "Contact")
    street = _isdoc_direct_text(address, "StreetName")
    building = _isdoc_direct_text(address, "BuildingNumber")
    if building and building not in street:
        street = (street + " " + building).strip()
    return ParsedParty(
        name=name,
        email=_isdoc_direct_text(contact, "ElectronicMail"),
        phone=_isdoc_direct_text(contact, "Telephone"),
        street=street,
        city=_isdoc_direct_text(address, "CityName"),
        zip=_isdoc_direct_text(address, "PostalZone"),
        country=(_isdoc_direct_text(country_el, "IdentificationCode") or "CZ").strip().upper() or "CZ",
        ico=_isdoc_direct_text(ident, "ID"),
        dic=_isdoc_direct_text(tax_scheme, "CompanyID"),
    )


def parse_isdoc_invoices_xml(xml_bytes: bytes) -> list[ParsedInvoice]:
    """Parse ISDOC/ISDOCX invoice XML into ParsedInvoice rows."""
    xml_bytes = ensure_safe_xml_bytes(xml_bytes)
    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné ISDOC XML: {exc}") from exc

    invoice_els = [root] if _norm(root.tag) == "invoice" else [el for el in root.iter() if _norm(el.tag) == "invoice"]
    invoices: list[ParsedInvoice] = []
    for inv_el in invoice_els:
        number = _isdoc_direct_text(inv_el, "ID") or f"ISDOC-{len(invoices)+1}"
        external_id = _isdoc_direct_text(inv_el, "UUID") or f"number:{number}"
        doc_type = _isdoc_direct_text(inv_el, "DocumentType")
        status = "open"
        issue_date = _parse_date(_isdoc_direct_text(inv_el, "IssueDate"))
        taxable_supply_date = _parse_date(_isdoc_direct_text(inv_el, "TaxPointDate")) or issue_date
        due_date = None
        payment = _first_descendant(_first_child(inv_el, "PaymentMeans"), "Details")
        if payment is not None:
            due_date = _parse_date(_isdoc_direct_text(payment, "PaymentDueDate"))
        if due_date is None:
            due_date = issue_date
        currency = (_isdoc_direct_text(inv_el, "LocalCurrencyCode") or "CZK").strip().upper() or "CZK"
        variable_symbol = digits_only(_isdoc_direct_text(payment, "VariableSymbol") if payment is not None else "")[:10]
        seller = _isdoc_party(_first_child(inv_el, "AccountingSupplierParty"))
        buyer = _isdoc_party(_first_child(inv_el, "AccountingCustomerParty"))
        note = _isdoc_direct_text(inv_el, "Note")

        bank_account = ""
        iban = ""
        bic = ""
        if payment is not None:
            account_id = _isdoc_direct_text(payment, "ID")
            bank_code = _isdoc_direct_text(payment, "BankCode")
            bank_account = f"{account_id}/{bank_code}" if account_id and bank_code else account_id
            iban = _isdoc_direct_text(payment, "IBAN")
            bic = _isdoc_direct_text(payment, "BIC")

        lines: list[ParsedLine] = []
        lines_root = _first_child(inv_el, "InvoiceLines")
        for line_el in list(lines_root or []):
            if _norm(line_el.tag) != "invoiceline":
                continue
            desc = _isdoc_direct_text(_first_child(line_el, "Item"), "Description") or "Položka"
            quantity_el = _first_child(line_el, "InvoicedQuantity")
            try:
                qty = parse_quantity(_txt(quantity_el) or "1")
            except Exception:
                qty = Decimal("1.00")
            unit_code = str((quantity_el.attrib.get("unitCode") if quantity_el is not None else "") or "").strip()
            try:
                unit_price_cents = int(parse_money_to_cents(_isdoc_direct_text(line_el, "UnitPrice") or "0"))
            except Exception:
                unit_price_cents = 0
            try:
                net_cents = int(parse_money_to_cents(_isdoc_direct_text(line_el, "LineExtensionAmount") or "0"))
            except Exception:
                net_cents = 0
            try:
                vat_cents = int(parse_money_to_cents(_isdoc_direct_text(line_el, "LineExtensionTaxAmount") or "0"))
            except Exception:
                vat_cents = 0
            try:
                total_cents_line = int(parse_money_to_cents(_isdoc_direct_text(line_el, "LineExtensionAmountTaxInclusive") or "0"))
            except Exception:
                total_cents_line = int(net_cents) + int(vat_cents)
            tax_category = _first_child(line_el, "ClassifiedTaxCategory")
            try:
                vat_rate = parse_vat_rate(_isdoc_direct_text(tax_category, "Percent") or "0")
            except Exception:
                vat_rate = Decimal("0.00")
            lines.append(
                ParsedLine(
                    description=desc,
                    quantity=qty,
                    unit_price_cents=unit_price_cents,
                    vat_rate=vat_rate,
                    net_cents=net_cents,
                    vat_cents=vat_cents,
                    total_cents=total_cents_line,
                    unit=unit_code,
                )
            )

        total_cents = None
        monetary_total = _first_child(inv_el, "LegalMonetaryTotal")
        payable = _isdoc_direct_text(monetary_total, "PayableAmount")
        if payable:
            try:
                total_cents = int(parse_money_to_cents(payable))
            except Exception:
                total_cents = None

        invoices.append(
            ParsedInvoice(
                external_id=external_id,
                number=number,
                variable_symbol=variable_symbol,
                status=status,
                issue_date=issue_date,
                due_date=due_date,
                currency=currency,
                taxable_supply_date=taxable_supply_date,
                note=note,
                private_note=("Import z ISDOC" + (f"; typ dokladu {doc_type}" if doc_type else "")),
                total_cents=total_cents,
                buyer_external_id=buyer.ico or None,
                buyer=buyer,
                seller=seller,
                lines=lines,
                sent_at=None,
                paid_on=None,
                bank_account=bank_account,
                iban=iban,
                bic=bic,
                payment_method="bank_transfer",
            )
        )
    return invoices


def parse_fakturoid_invoices_xml(xml_bytes: bytes) -> list[ParsedInvoice]:
    """Parse Fakturoid XML export into a list of invoices.

    Supports either <invoice> root or <invoices><invoice>...</invoice></invoices>.
    Tag names are normalized by lowercasing and converting '-' to '_'.
    """

    xml_bytes = ensure_safe_xml_bytes(xml_bytes)

    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné XML: {exc}") from exc

    root_tag = _norm(root.tag)

    invoice_els: list[ET.Element]
    if root_tag == "invoice":
        invoice_els = [root]
    else:
        # Prefer direct children of an <invoices> node if present.
        if root_tag == "invoices":
            invoice_els = [ch for ch in list(root) if _norm(ch.tag) == "invoice"]
        else:
            invoice_els = [el for el in root.iter() if _norm(el.tag) == "invoice"]

    invoices: list[ParsedInvoice] = []
    for inv_el in invoice_els:
        fields = _child_text_map(inv_el)

        inv_id = fields.get("id") or ""
        number = fields.get("number") or fields.get("invoice_number") or ""
        if not inv_id and number:
            inv_id = f"number:{number}"
        if not inv_id:
            # Keep deterministic fallback; still importable but not perfectly idempotent.
            inv_id = f"anon:{len(invoices)+1}"

        variable_symbol = digits_only(
            fields.get("variable_symbol")
            or fields.get("variable_number")
            or fields.get("payment_reference")
            or ""
        )[:10]

        status = (fields.get("status") or fields.get("state") or "open").strip().lower()

        issued_on = fields.get("issued_on") or fields.get("issue_date") or fields.get("issued")
        due_on = fields.get("due_on") or fields.get("due_date")
        issue_date = _parse_date(issued_on or "")
        due_date = _parse_date(due_on or "")

        currency = (fields.get("currency") or "CZK").strip().upper() or "CZK"
        bank_account = (fields.get("bank_account") or fields.get("account_number") or "").strip()
        iban = (fields.get("iban") or "").strip()
        bic = (fields.get("swift_bic") or fields.get("bic") or "").strip()
        payment_method = _map_import_payment_method(fields.get("payment_method") or fields.get("payment_method_human") or "")

        note = fields.get("note") or fields.get("notes") or ""
        private_note = fields.get("private_note") or fields.get("internal_note") or ""

        total_cents: int | None = None
        if (fields.get("total") or "").strip():
            try:
                total_cents = int(parse_money_to_cents(fields.get("total") or "0"))
            except Exception:
                total_cents = None

        buyer_external_id = (fields.get("subject_id") or fields.get("client_id") or "").strip() or None

        buyer = ParsedParty(
            name=(fields.get("client_name") or fields.get("buyer_name") or "").strip(),
            email=(fields.get("client_email") or "").strip(),
            phone=(fields.get("client_phone") or "").strip(),
            street=(fields.get("client_street") or "").strip(),
            city=(fields.get("client_city") or "").strip(),
            zip=(fields.get("client_zip") or "").strip(),
            country=(fields.get("client_country") or "CZ").strip().upper() or "CZ",
            ico=(fields.get("client_registration_no") or fields.get("client_ico") or "").strip(),
            dic=(fields.get("client_vat_no") or fields.get("client_dic") or "").strip(),
        )

        seller_name = (fields.get("your_name") or fields.get("seller_name") or "").strip()
        seller: ParsedParty | None = None
        if seller_name or (fields.get("your_street") or "").strip():
            seller = ParsedParty(
                name=seller_name,
                email=(fields.get("your_email") or "").strip(),
                phone=(fields.get("your_phone") or "").strip(),
                street=(fields.get("your_street") or "").strip(),
                city=(fields.get("your_city") or "").strip(),
                zip=(fields.get("your_zip") or "").strip(),
                country=(fields.get("your_country") or "CZ").strip().upper() or "CZ",
                ico=(fields.get("your_registration_no") or fields.get("your_ico") or "").strip(),
                dic=(fields.get("your_vat_no") or fields.get("your_dic") or "").strip(),
            )

        sent_at = _parse_datetime(fields.get("sent_at") or "")
        paid_on = _parse_date(fields.get("paid_on") or "")

        # Parse lines
        lines: list[ParsedLine] = []

        # Find container(s)
        containers: list[ET.Element] = []
        for ch in list(inv_el):
            if _norm(ch.tag) in {"lines", "line_items", "items"}:
                containers.append(ch)
        if not containers:
            # Sometimes lines can be nested deeper.
            for el in inv_el.iter():
                if _norm(el.tag) in {"lines", "line_items", "items"}:
                    containers.append(el)
                    break

        for cont in containers:
            for line_el in list(cont):
                if _norm(line_el.tag) not in {"line", "item", "invoice_line", "invoice_item"}:
                    # Some exports may use <lines><hash>...</hash></lines>, ignore unknown nodes.
                    continue
                lf = _child_text_map(line_el)
                desc = (lf.get("name") or lf.get("description") or lf.get("text") or "").strip()
                if not desc:
                    # Avoid empty lines; still keep deterministic placeholder.
                    desc = "Položka"

                try:
                    qty = parse_quantity(lf.get("quantity") or "1")
                except Exception:
                    qty = Decimal("1.00")

                try:
                    unit_price_cents = int(parse_money_to_cents(lf.get("unit_price") or "0"))
                except Exception:
                    unit_price_cents = 0

                try:
                    vat_rate = parse_vat_rate(lf.get("vat_rate") or "0")
                except Exception:
                    vat_rate = Decimal("0.00")

                # Prefer totals from export if present.
                net_cents: int | None = None
                vat_cents: int | None = None
                total_cents_line: int | None = None

                if (lf.get("total_price_without_vat") or "").strip():
                    try:
                        net_cents = int(parse_money_to_cents(lf.get("total_price_without_vat") or "0"))
                    except Exception:
                        net_cents = None
                if (lf.get("total_vat") or "").strip():
                    try:
                        vat_cents = int(parse_money_to_cents(lf.get("total_vat") or "0"))
                    except Exception:
                        vat_cents = None
                if (lf.get("total_price_with_vat") or "").strip():
                    try:
                        total_cents_line = int(parse_money_to_cents(lf.get("total_price_with_vat") or "0"))
                    except Exception:
                        total_cents_line = None

                if net_cents is not None and vat_cents is not None:
                    total = int(net_cents) + int(vat_cents)
                    lines.append(
                        ParsedLine(
                            description=desc,
                            quantity=qty,
                            unit_price_cents=unit_price_cents,
                            vat_rate=vat_rate,
                            net_cents=int(net_cents),
                            vat_cents=int(vat_cents),
                            total_cents=int(total_cents_line or total),
                        )
                    )
                else:
                    net, vat, total = compute_line_amounts_cents(
                        quantity=qty,
                        unit_price_cents=unit_price_cents,
                        vat_rate=vat_rate,
                    )
                    lines.append(
                        ParsedLine(
                            description=desc,
                            quantity=qty,
                            unit_price_cents=unit_price_cents,
                            vat_rate=vat_rate,
                            net_cents=net,
                            vat_cents=vat,
                            total_cents=total,
                        )
                    )

        invoices.append(
            ParsedInvoice(
                external_id=inv_id,
                number=number or inv_id,
                variable_symbol=variable_symbol,
                status=status,
                issue_date=issue_date,
                taxable_supply_date=issue_date,
                due_date=due_date,
                currency=currency,
                note=note,
                private_note=private_note,
                total_cents=total_cents,
                buyer_external_id=buyer_external_id,
                buyer=buyer,
                seller=seller,
                lines=lines,
                sent_at=sent_at,
                paid_on=paid_on,
                bank_account=bank_account,
                iban=iban,
                bic=bic,
                payment_method=payment_method,
            )
        )

    return invoices


def parse_fakturoid_contacts_xml(xml_bytes: bytes) -> list[ParsedContact]:
    """Parse Fakturoid-style XML contact export.

    Supports either a single contact root or a container with repeated
    <contact>/<subject>/<client> items. The parser is intentionally tolerant to
    slight schema variations.
    """

    xml_bytes = ensure_safe_xml_bytes(xml_bytes)

    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné XML: {exc}") from exc

    root_tag = _norm(root.tag)
    contact_tags = {"contact", "subject", "client"}

    if root_tag in contact_tags:
        contact_els = [root]
    elif root_tag in {"contacts", "subjects", "clients", "address_book"}:
        contact_els = [ch for ch in list(root) if _norm(ch.tag) in contact_tags]
    else:
        contact_els = [el for el in root.iter() if _norm(el.tag) in contact_tags]

    contacts: list[ParsedContact] = []
    for contact_el in contact_els:
        fields = _flatten_text_map(contact_el)
        direct_fields = _child_text_map(contact_el)
        merged_fields = {**fields, **direct_fields}
        contacts.append(_parsed_contact_from_field_map(merged_fields))

    return contacts


def _safe_decimal_cents(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(parse_money_to_cents(raw))
    except Exception:
        return None


def _safe_decimal_quantity(value: str | None) -> Decimal:
    raw = str(value or "").strip()
    if not raw:
        return Decimal("1.00")
    try:
        return parse_quantity(raw.replace(",", "."))
    except Exception:
        return Decimal("1.00")


def _safe_decimal_vat_rate(value: str | None) -> Decimal:
    raw = str(value or "").strip()
    if not raw:
        return Decimal("0.00")
    try:
        return parse_vat_rate(raw.replace("%", ""))
    except Exception:
        return Decimal("0.00")


def parse_pohoda_invoices_xml(xml_bytes: bytes) -> list[ParsedInvoice]:
    """Parse POHODA invoice XML export into ParsedInvoice rows."""

    xml_bytes = ensure_safe_xml_bytes(xml_bytes)
    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné XML: {exc}") from exc

    invoices: list[ParsedInvoice] = []
    for inv_idx, inv_el in enumerate(_iter_descendants(root, "invoice"), start=1):
        header = _first_child(inv_el, "invoiceHeader")
        if header is None:
            header = inv_el
        detail = _first_child(inv_el, "invoiceDetail")
        summary = _first_child(inv_el, "invoiceSummary")

        header_fields = _flatten_text_map(header)
        partner_address = _first_descendant(header, "address")
        partner_fields = _flatten_text_map(partner_address) if partner_address is not None else {}
        account_el = _first_child(header, "account")
        account_fields = _flatten_text_map(account_el) if account_el is not None else {}

        number = (
            _txt(_first_descendant(header, "numberRequested", "number"))
            or header_fields.get("numberrequested")
            or header_fields.get("number")
            or f"POHODA-{inv_idx}"
        ).strip()
        variable_symbol = digits_only(
            header_fields.get("symvar")
            or header_fields.get("variablesymbol")
            or number
        )[:10]
        note = header_fields.get("note") or header_fields.get("text") or ""
        payment_method = _map_import_payment_method(header_fields.get("paymenttype") or "")
        issue_date = _parse_date(header_fields.get("date") or header_fields.get("datetax") or "")
        due_date = _parse_date(header_fields.get("datedue") or "")
        currency = (header_fields.get("currency") or "CZK").strip().upper() or "CZK"

        buyer = ParsedParty(
            name=(partner_fields.get("company") or partner_fields.get("name") or "").strip(),
            email=(partner_fields.get("email") or "").strip(),
            phone=(partner_fields.get("mobilphone") or partner_fields.get("phone") or "").strip(),
            street=(partner_fields.get("street") or "").strip(),
            city=(partner_fields.get("city") or "").strip(),
            zip=(partner_fields.get("zip") or "").strip(),
            country=((partner_fields.get("country") or "CZ").strip().upper() or "CZ")[:2],
            ico=(partner_fields.get("ico") or "").strip(),
            dic=(partner_fields.get("dic") or "").strip(),
        )

        lines: list[ParsedLine] = []
        for item_el in _iter_descendants(detail, "invoiceItem"):
            item_fields = _flatten_text_map(item_el)
            home_currency = _first_child(item_el, "homeCurrency")
            money_fields = _flatten_text_map(home_currency) if home_currency is not None else {}
            desc = (item_fields.get("text") or item_fields.get("description") or "Položka").strip()
            qty = _safe_decimal_quantity(item_fields.get("quantity"))
            unit = (item_fields.get("unit") or "").strip()
            unit_price_cents = _safe_decimal_cents(money_fields.get("unitprice") or item_fields.get("unitprice")) or 0
            vat_rate = _safe_decimal_vat_rate(item_fields.get("ratevat"))
            net_cents = _safe_decimal_cents(money_fields.get("price") or item_fields.get("price"))
            total_cents_line = _safe_decimal_cents(money_fields.get("pricesum") or item_fields.get("pricesum"))
            if net_cents is None or total_cents_line is None:
                net, vat, total = compute_line_amounts_cents(
                    quantity=qty,
                    unit_price_cents=unit_price_cents,
                    vat_rate=vat_rate,
                )
            else:
                vat = int(total_cents_line) - int(net_cents)
                net = int(net_cents)
                total = int(total_cents_line)
            lines.append(
                ParsedLine(
                    description=desc,
                    quantity=qty,
                    unit_price_cents=unit_price_cents,
                    vat_rate=vat_rate,
                    net_cents=net,
                    vat_cents=vat,
                    total_cents=total,
                    unit=unit,
                )
            )

        total_cents = _safe_decimal_cents(_flatten_text_map(summary).get("pricenone")) if summary is not None else None
        if total_cents is None and lines:
            total_cents = sum(int(line.total_cents) for line in lines)

        invoices.append(
            ParsedInvoice(
                external_id=f"pohoda:{number}",
                number=number,
                variable_symbol=variable_symbol,
                status="issued",
                issue_date=issue_date,
                taxable_supply_date=issue_date,
                due_date=due_date,
                currency=currency,
                note=note,
                private_note="",
                total_cents=total_cents,
                buyer_external_id=None,
                buyer=buyer,
                seller=None,
                lines=lines,
                sent_at=None,
                paid_on=None,
                bank_account=(account_fields.get("accountno") or "").strip(),
                iban=(account_fields.get("iban") or "").strip(),
                bic=(account_fields.get("swift") or "").strip(),
                payment_method=payment_method,
            )
        )

    return invoices


def parse_money_s3_invoices_xml(xml_bytes: bytes) -> list[ParsedInvoice]:
    """Parse Money S3 XML export into ParsedInvoice rows."""

    xml_bytes = ensure_safe_xml_bytes(xml_bytes)
    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné XML: {exc}") from exc

    invoice_nodes = _iter_descendants(root, "FaktVyd")
    if _norm(root.tag) == "faktvyd":
        invoice_nodes = [root]

    invoices: list[ParsedInvoice] = []
    for inv_idx, inv_el in enumerate(invoice_nodes, start=1):
        fields = _flatten_text_map(inv_el)
        partner_el = _first_child(inv_el, "Partner")
        partner_fields = _flatten_text_map(partner_el) if partner_el is not None else {}
        account_el = _first_child(inv_el, "BankovniUcet")
        account_fields = _flatten_text_map(account_el) if account_el is not None else {}

        number = (fields.get("doklad") or fields.get("number") or f"MONEY-{inv_idx}").strip()
        variable_symbol = digits_only(fields.get("varsymbol") or number)[:10]
        status = (fields.get("stav") or "issued").strip().lower() or "issued"
        note = fields.get("popis") or fields.get("note") or ""
        payment_method = _map_import_payment_method(fields.get("zpusobuhrady") or "")
        issue_date = _parse_date(fields.get("vystaveno") or "")
        due_date = _parse_date(fields.get("splatnost") or "")
        paid_on = _parse_date(fields.get("datumuhrady") or "")
        currency = (fields.get("mena") or "CZK").strip().upper() or "CZK"

        buyer = ParsedParty(
            name=(partner_fields.get("nazev") or partner_fields.get("name") or "").strip(),
            email=(partner_fields.get("email") or "").strip(),
            phone=(partner_fields.get("telefon") or partner_fields.get("phone") or "").strip(),
            street=(partner_fields.get("ulice") or partner_fields.get("street") or "").strip(),
            city=(partner_fields.get("mesto") or partner_fields.get("city") or "").strip(),
            zip=(partner_fields.get("psc") or partner_fields.get("zip") or "").strip(),
            country=((partner_fields.get("stat") or "CZ").strip().upper() or "CZ")[:2],
            ico=(partner_fields.get("ico") or "").strip(),
            dic=(partner_fields.get("dic") or "").strip(),
        )

        lines: list[ParsedLine] = []
        items_el = _first_child(inv_el, "Polozky")
        for item_el in _iter_descendants(items_el, "Polozka"):
            item_fields = _flatten_text_map(item_el)
            desc = (item_fields.get("nazev") or item_fields.get("text") or "Položka").strip()
            qty = _safe_decimal_quantity(item_fields.get("mnozstvi"))
            unit = (item_fields.get("mj") or item_fields.get("unit") or "").strip()
            unit_price_cents = _safe_decimal_cents(item_fields.get("cenamj")) or 0
            vat_rate = _safe_decimal_vat_rate(item_fields.get("sazbadph"))
            total_cents_line = _safe_decimal_cents(item_fields.get("cenacelkem"))
            if total_cents_line is None:
                net, vat, total = compute_line_amounts_cents(
                    quantity=qty,
                    unit_price_cents=unit_price_cents,
                    vat_rate=vat_rate,
                )
            else:
                total = int(total_cents_line)
                if vat_rate > Decimal("0.00"):
                    net, vat, _computed_total = compute_line_amounts_cents(
                        quantity=qty,
                        unit_price_cents=unit_price_cents,
                        vat_rate=vat_rate,
                    )
                    if total != _computed_total:
                        vat = max(0, total - net)
                else:
                    net, vat = total, 0
            lines.append(
                ParsedLine(
                    description=desc,
                    quantity=qty,
                    unit_price_cents=unit_price_cents,
                    vat_rate=vat_rate,
                    net_cents=int(net),
                    vat_cents=int(vat),
                    total_cents=int(total),
                    unit=unit,
                )
            )

        total_cents = _safe_decimal_cents(fields.get("celkem"))
        if total_cents is None and lines:
            total_cents = sum(int(line.total_cents) for line in lines)

        invoices.append(
            ParsedInvoice(
                external_id=f"money_s3:{number}",
                number=number,
                variable_symbol=variable_symbol,
                status=status,
                issue_date=issue_date,
                taxable_supply_date=issue_date,
                due_date=due_date,
                currency=currency,
                note=note,
                private_note="",
                total_cents=total_cents,
                buyer_external_id=None,
                buyer=buyer,
                seller=None,
                lines=lines,
                sent_at=None,
                paid_on=paid_on,
                bank_account=(account_fields.get("cislouctu") or "").strip(),
                iban=(account_fields.get("iban") or "").strip(),
                bic=(account_fields.get("bic") or "").strip(),
                payment_method=payment_method,
            )
        )

    return invoices


def _parse_cz_date(value: str) -> date | None:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    m = re.search(r"(\d{2})\.\s*(\d{2})\.\s*(\d{4})", raw)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except Exception:
        return None


def _normalize_pdf_lines(text: str) -> list[str]:
    raw_lines = [re.sub(r"\s+", " ", ln.replace("\xa0", " ")).strip() for ln in str(text or "").splitlines()]
    raw_lines = [ln for ln in raw_lines if ln]

    out: list[str] = []
    i = 0
    while i < len(raw_lines):
        cur = raw_lines[i]
        nxt = raw_lines[i + 1] if i + 1 < len(raw_lines) else ""
        if re.fullmatch(r"\d{1,3}(?: \d{3})*", cur) and re.fullmatch(r"\d{3},\d{2}\s*[KJ]č", nxt):
            out.append(f"{cur} {nxt.replace('Jč', 'Kč')}")
            i += 2
            continue
        out.append(cur.replace("Jč", "Kč"))
        i += 1
    return out


def _extract_pdf_text_lines(pdf_bytes: bytes) -> list[str]:
    if PdfReader is None:  # pragma: no cover
        raise ValueError(f"PDF import vyžaduje balíček pypdf ({_PYPDF_IMPORT_ERROR})")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ValueError(f"Neplatné PDF: {exc}") from exc

    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    lines = _normalize_pdf_lines(text)
    if not lines:
        raise ValueError("PDF neobsahuje čitelný text")
    return lines


def _looks_like_ico(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6,10}", str(value or "").strip()))


def _looks_like_dic(value: str) -> bool:
    raw = str(value or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z]{2}\d{6,12}", raw))


def _parse_city_zip_line(value: str) -> tuple[str, str]:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    m = re.match(r"(?P<zip>\d{3}\s?\d{2})\s+(?P<city>.+)$", raw)
    if not m:
        return "", raw
    return m.group("zip"), m.group("city").strip()


def parse_fakturoid_invoice_pdf(pdf_bytes: bytes, *, filename: str = "invoice.pdf") -> ParsedInvoice:
    """Parse one invoice PDF exported from Fakturoid/Fakturek into ParsedInvoice."""

    lines = _extract_pdf_text_lines(pdf_bytes)
    full_text = "\n".join(lines)

    number_match = re.search(r"\b\d{4}-\d{4,}\b", full_text)
    number = number_match.group(0) if number_match else Path(str(filename or "invoice.pdf")).stem
    variable_symbol_match = re.search(r"Variabilní symbol\s+(\d{1,10})", full_text)
    variable_symbol = digits_only(variable_symbol_match.group(1) if variable_symbol_match else "")[:10]

    issue_match = re.search(r"Datum vystavení\s+(\d{2}\.\s*\d{2}\.\s*\d{4})", full_text)
    due_match = re.search(r"Datum splatnosti\s+(\d{2}\.\s*\d{2}\.\s*\d{4})", full_text)
    issue_date = _parse_cz_date(issue_match.group(1) if issue_match else "")
    due_date = _parse_cz_date(due_match.group(1) if due_match else "")

    account_idx = next((i for i, ln in enumerate(lines) if re.fullmatch(r"\d{6,}/\d{4}", ln)), None)
    buyer = ParsedParty()
    if account_idx is not None:
        buyer_tail = lines[:account_idx]
        j = len(buyer_tail) - 1
        buyer_dic = ""
        if j >= 0 and _looks_like_dic(buyer_tail[j]):
            buyer_dic = buyer_tail[j]
            j -= 1
        buyer_ico = ""
        if j >= 0 and _looks_like_ico(buyer_tail[j]):
            buyer_ico = buyer_tail[j]
            j -= 1
        buyer_city_zip = buyer_tail[j] if j >= 0 else ""
        if j >= 0:
            j -= 1
        buyer_street = buyer_tail[j] if j >= 0 else ""
        if j >= 0:
            j -= 1
        buyer_name = buyer_tail[j] if j >= 0 else ""
        zip_code, city = _parse_city_zip_line(buyer_city_zip)
        buyer = ParsedParty(
            name=buyer_name,
            street=buyer_street,
            city=city,
            zip=zip_code,
            country="CZ",
            ico=buyer_ico,
            dic=buyer_dic,
        )

    item_header_idx = next((i for i, ln in enumerate(lines) if "CENA ZA MJ" in ln and "CELKEM" in ln), None)
    footer_markers = ("Fyzická osoba", "Fakturu vyrobil robot", "QR Platba", "Sumář", "Obsah")
    candidate_lines: list[str] = []
    if item_header_idx is not None:
        for ln in lines[item_header_idx + 1 :]:
            if any(ln.startswith(marker) for marker in footer_markers):
                break
            candidate_lines.append(ln)

    item_re = re.compile(
        r"^(?P<qty>\d+(?:[.,]\d+)?)\s+"
        r"(?P<unit>\S+)\s+"
        r"(?P<desc>.+?)\s+"
        r"(?P<unit_price>\d[\d ]*,\d{2})\s*(?:K.|CZK)\s+"
        r"(?P<total>\d[\d ]*,\d{2})\s*(?:K.|CZK)$"
    )

    parsed_lines: list[ParsedLine] = []
    buffer: list[str] = []
    for ln in candidate_lines:
        buffer.append(ln)
        joined = " ".join(buffer)
        match = item_re.search(joined)
        if not match:
            continue
        qty = parse_quantity(match.group("qty").replace(",", "."))
        unit_price_cents = int(parse_money_to_cents(match.group("unit_price")))
        total_cents = int(parse_money_to_cents(match.group("total")))
        description = match.group("desc").strip()
        if match.group("unit").strip():
            description = f"{description} ({match.group('unit').strip()})"
        net, vat, _ = compute_line_amounts_cents(
            quantity=qty,
            unit_price_cents=unit_price_cents,
            vat_rate=Decimal("0.00"),
        )
        parsed_lines.append(
            ParsedLine(
                description=description,
                quantity=qty,
                unit_price_cents=unit_price_cents,
                vat_rate=Decimal("0.00"),
                net_cents=net,
                vat_cents=vat,
                total_cents=total_cents,
            )
        )
        buffer = []

    if not parsed_lines:
        raise ValueError(f"Z PDF {filename} se nepodařilo načíst žádné položky")

    total_cents = sum(int(line.total_cents or 0) for line in parsed_lines)

    return ParsedInvoice(
        external_id=f"pdf:{number}",
        number=number,
        variable_symbol=variable_symbol,
        status="sent",
        issue_date=issue_date,
        due_date=due_date,
        currency="CZK",
        note="",
        private_note="",
        total_cents=total_cents,
        buyer_external_id=None,
        buyer=buyer,
        seller=None,
        lines=parsed_lines,
        sent_at=None,
        paid_on=None,
        bank_account="",
        iban="",
        bic="",
        payment_method="bank_transfer",
    )


# ---------------------------------------------------------------------------
# Contacts CSV parsing
# ---------------------------------------------------------------------------


def _norm_csv_header(value: str) -> str:
    v = (value or "").strip().lower()
    v = v.replace("-", "_")
    v = re.sub(r"\s+", "_", v)
    v = re.sub(r"[^a-z0-9_]+", "_", v)
    v = re.sub(r"_+", "_", v).strip("_")
    return v


def _detect_csv_delimiter(sample: str) -> str:
    """Detect CSV delimiter with a safe fallback."""

    candidates = [",", ";", "\t"]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=candidates)
        if getattr(dialect, "delimiter", None) in candidates:
            return str(dialect.delimiter)
    except Exception:
        pass

    # Heuristic: count delimiters in the header line.
    first = (sample.splitlines()[:1] or [""])[0]
    best = ","
    best_count = -1
    for d in candidates:
        c = first.count(d)
        if c > best_count:
            best = d
            best_count = c
    return best


CONTACT_CSV_TARGET_FIELDS: tuple[str, ...] = (
    "external_id",
    "name",
    "email",
    "phone",
    "street",
    "city",
    "zip",
    "country",
    "ico",
    "dic",
    "fixed_variable_symbol",
)

CONTACT_CSV_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": ("id", "subject_id", "client_id", "contact_id"),
    "name": ("name", "company", "company_name", "subject_name", "client_name", "full_name", "first_name", "firstname"),
    "email": ("email", "mail"),
    "phone": ("phone", "telephone", "mobile"),
    "street": ("street", "address", "address_street", "line_1", "line1"),
    "city": ("city", "town"),
    "zip": ("zip", "zipcode", "postal_code", "psc"),
    "country": ("country",),
    "ico": ("registration_no", "ico", "company_registration_no", "ico_dph"),
    "dic": ("vat_no", "dic", "vat_number"),
    "fixed_variable_symbol": ("variable_symbol", "default_variable_symbol", "payment_reference"),
}


def _read_csv_dict_rows(csv_bytes: bytes) -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
    try:
        text = csv_bytes.decode("utf-8-sig")
    except Exception:
        text = csv_bytes.decode("utf-8", errors="replace")

    if not text.strip():
        return [], {}, []

    sample = text[:4096]
    delimiter = _detect_csv_delimiter(sample)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV nemá hlavičku")
    fieldnames = [str(value or "").strip() for value in reader.fieldnames]
    field_map = {fn: _norm_csv_header(fn) for fn in fieldnames}
    rows: list[dict[str, str]] = []
    for row in reader:
        if row is None:
            continue
        normalized_row = {str(key or ""): str(value or "").strip() for key, value in row.items()}
        if not any(normalized_row.values()):
            continue
        rows.append(normalized_row)
    return fieldnames, field_map, rows


def infer_contact_csv_mapping(fieldnames: list[str]) -> dict[str, str]:
    field_map = {fn: _norm_csv_header(fn) for fn in fieldnames}
    inverse = {normalized: original for original, normalized in field_map.items()}
    mapping: dict[str, str] = {}
    for target, aliases in CONTACT_CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in inverse:
                mapping[target] = inverse[alias]
                break
    return mapping


def inspect_contacts_csv(csv_bytes: bytes) -> dict[str, object]:
    fieldnames, field_map, rows = _read_csv_dict_rows(csv_bytes)
    inferred_mapping = infer_contact_csv_mapping(fieldnames)
    sample_rows = rows[:3]
    return {
        "headers": fieldnames,
        "normalized_headers": field_map,
        "row_count": len(rows),
        "sample_rows": sample_rows,
        "inferred_mapping": inferred_mapping,
    }


def _field_value_from_mapping(
    row: dict[str, str],
    *,
    field_map: dict[str, str],
    mapping_value: str | None,
    aliases: tuple[str, ...],
) -> str:
    requested = str(mapping_value or "").strip()
    if requested:
        requested_norm = _norm_csv_header(requested)
        for original, normalized in field_map.items():
            if original == requested or normalized == requested_norm:
                value = str(row.get(original) or "").strip()
                if value:
                    return value
        return ""

    for alias in aliases:
        for original, normalized in field_map.items():
            if normalized == alias:
                value = str(row.get(original) or "").strip()
                if value:
                    return value
    return ""


def parse_contacts_csv_with_mapping(csv_bytes: bytes, mapping: dict[str, str] | None = None) -> list[ParsedContact]:
    fieldnames, field_map, rows = _read_csv_dict_rows(csv_bytes)
    if not fieldnames:
        return []
    selected_mapping = {str(key): str(value) for key, value in (mapping or {}).items() if str(value or "").strip()}

    contacts: list[ParsedContact] = []
    for row in rows:
        selected: dict[str, str] = {}
        for target, aliases in CONTACT_CSV_FIELD_ALIASES.items():
            selected[target] = _field_value_from_mapping(
                row,
                field_map=field_map,
                mapping_value=selected_mapping.get(target),
                aliases=aliases,
            )
        field_values = {
            "id": selected.get("external_id", ""),
            "name": selected.get("name", ""),
            "email": selected.get("email", ""),
            "phone": selected.get("phone", ""),
            "street": selected.get("street", ""),
            "city": selected.get("city", ""),
            "zip": selected.get("zip", ""),
            "country": selected.get("country", ""),
            "ico": selected.get("ico", ""),
            "dic": selected.get("dic", ""),
            "variable_symbol": selected.get("fixed_variable_symbol", ""),
        }
        contacts.append(_parsed_contact_from_field_map(field_values))
    return contacts


def parse_fakturoid_contacts_csv(csv_bytes: bytes) -> list[ParsedContact]:
    """Parse Fakturoid contacts export in CSV format.

    The parser is intentionally tolerant to header variations.
    """
    return parse_contacts_csv_with_mapping(csv_bytes, mapping=None)


# ---------------------------------------------------------------------------
# Invoice status mapping
# ---------------------------------------------------------------------------


def _map_invoice_status(value: str) -> str:
    s = (value or "").strip().lower()
    if s in {"paid"}:
        return "paid"
    if s in {"sent"}:
        return "sent"
    if s in {"overdue"}:
        # No explicit overdue state in Fakturek yet.
        return "sent"
    if s in {"draft", "concept"}:
        return "draft"
    if s in {"open"}:
        return "issued"
    # cancelled/uncollectible/... -> issued with a note.
    return "issued"


# ---------------------------------------------------------------------------
# Series inference (pure helpers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferredSeries:
    prefix: str
    pad_length: int
    max_counter: int


def split_invoice_number_prefix_counter(number: str) -> tuple[str, int, int] | None:
    """Split invoice number into (prefix, counter, digits_len).

    We look for a trailing digit sequence. If absent, returns None.

    Examples:
      "2023-0021" -> ("2023-", 21, 4)
      "0007" -> ("", 7, 4)
    """

    n = (number or "").strip()
    if not n:
        return None
    m = re.search(r"(\d+)$", n)
    if not m:
        return None
    digits = m.group(1)
    prefix = n[: -len(digits)]
    try:
        counter = int(digits)
    except Exception:
        return None
    return prefix, counter, len(digits)


def infer_series_from_invoice_numbers(numbers: list[str]) -> dict[str, InferredSeries]:
    """Infer series specs from invoice numbers.

    Returns a mapping prefix -> InferredSeries.
    """

    min_digits: dict[str, int] = {}
    max_counter: dict[str, int] = {}

    for num in numbers:
        parts = split_invoice_number_prefix_counter(num)
        if parts is None:
            continue
        prefix, counter, digits_len = parts
        if prefix not in min_digits:
            min_digits[prefix] = digits_len
        else:
            min_digits[prefix] = min(int(min_digits[prefix]), int(digits_len))
        prev = max_counter.get(prefix)
        if prev is None or int(counter) > int(prev):
            max_counter[prefix] = int(counter)

    out: dict[str, InferredSeries] = {}
    for prefix, mc in max_counter.items():
        pad = int(min_digits.get(prefix, 1) or 1)
        pad = max(1, min(pad, 20))
        out[prefix] = InferredSeries(prefix=str(prefix), pad_length=pad, max_counter=int(mc))

    return out


# ---------------------------------------------------------------------------
# DB-side helpers
# ---------------------------------------------------------------------------


def _safe_resolve_under_root(root: Path, rel: str) -> Path:
    base = root.resolve()
    p = (base / (rel or "")).resolve()
    if p == base:
        raise ValueError("Prázdná cesta k import souboru")
    if not p.is_relative_to(base):
        raise ValueError("Cesta k souboru je mimo import storage")
    return p


def _load_run_config(run) -> dict[str, object]:
    try:
        payload = json.loads(str(getattr(run, "summary_json", "") or ""))
    except Exception:
        return {}
    if isinstance(payload, dict):
        config = payload.get("config")
        if isinstance(config, dict):
            return dict(config)
    return {}


def _load_import_payload_bytes(*, run_file_path: str, storage_root: Path) -> tuple[str, bytes]:
    path = _safe_resolve_under_root(storage_root, run_file_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Import soubor neexistuje: {path}")
    data = path.read_bytes()
    return path.name, data


def detect_xml_import_format(xml_bytes: bytes) -> str:
    xml_bytes = ensure_safe_xml_bytes(xml_bytes)
    try:
        root = _safe_xml_fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Neplatné XML: {exc}") from exc

    root_tag = _norm(root.tag)
    if root_tag == "invoice" and (str(root.tag).startswith("{http://isdoc.cz/") or str(root.attrib.get("version") or "").strip()):
        return "isdoc"
    if root_tag in {"datapack", "invoice"} and any(_norm(node.tag) == "invoiceheader" for node in root.iter()):
        return "pohoda_xml"
    if root_tag == "moneydata" or any(_norm(node.tag) == "faktvyd" for node in root.iter()):
        return "money_s3_xml"
    if root_tag in {"invoices", "invoice", "contacts", "subjects", "clients", "address_book"}:
        return "fakturoid"
    return "invoice_xml"


def _select_invoice_parser(source: str, xml_bytes: bytes):
    normalized = str(source or "fakturoid").strip().lower()
    if normalized == "pohoda_xml":
        return parse_pohoda_invoices_xml, "pohoda_xml"
    if normalized == "money_s3_xml":
        return parse_money_s3_invoices_xml, "money_s3_xml"
    if normalized in {"isdoc", "isdoc_xml"}:
        return parse_isdoc_invoices_xml, "isdoc"
    if normalized == "invoice_xml":
        detected = detect_xml_import_format(xml_bytes)
        if detected == "pohoda_xml":
            return parse_pohoda_invoices_xml, detected
        if detected == "money_s3_xml":
            return parse_money_s3_invoices_xml, detected
        if detected == "isdoc":
            return parse_isdoc_invoices_xml, detected
    return parse_fakturoid_invoices_xml, normalized


def _suggest_series_name(prefix: str) -> str:
    p = (prefix or "").strip()
    if not p:
        return "default"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-").lower()
    slug = slug[:50] if slug else "import"
    return f"import-{slug}"


def _unique_series_name(existing: set[str], desired: str) -> str:
    name = (desired or "import").strip()[:100] or "import"
    if name not in existing:
        existing.add(name)
        return name
    base = name[:90]  # reserve room for suffix
    for i in range(2, 10_000):
        cand = f"{base}-{i}"[:100]
        if cand not in existing:
            existing.add(cand)
            return cand
    # Extremely unlikely.
    raise ValueError("Nepodařilo se vygenerovat unikátní název číselné řady")


def reconcile_invoice_series_for_subject(db, *, subject_id: int) -> dict:
    """Best-effort series reconciliation.

    - ensures InvoiceSeries rows exist for prefixes found in invoice numbers
    - bumps last_counter to max observed
    - assigns series_id to invoices that don't have it set

    The function mutates DB state but does not commit.
    """

    from sqlalchemy import select

    from fakturek.models import Invoice, InvoiceSeries

    subject_id = int(subject_id)

    series_rows = db.scalars(select(InvoiceSeries).where(InvoiceSeries.subject_id == subject_id)).all()
    existing_names = {str(s.name) for s in series_rows}

    # Group existing series by prefix.
    by_prefix: dict[str, list[InvoiceSeries]] = {}
    for s in series_rows:
        by_prefix.setdefault(str(s.prefix or ""), []).append(s)

    # Pull invoice numbers.
    inv_rows = db.execute(
        select(Invoice.id, Invoice.number, Invoice.series_id).where(Invoice.subject_id == subject_id)
    ).all()

    numbers = [str(num or "") for (_id, num, _sid) in inv_rows]
    inferred = infer_series_from_invoice_numbers(numbers)

    created_series = 0
    updated_series = 0
    assigned_invoices = 0

    # Ensure series exist.
    prefix_to_series: dict[str, InvoiceSeries] = {}
    for prefix, spec in inferred.items():
        candidates = by_prefix.get(prefix) or []
        if candidates:
            # Deterministic pick: prefer name 'default' then highest pad_length then lowest id.
            candidates.sort(
                key=lambda s: (
                    0 if str(s.name) == "default" else 1,
                    -int(getattr(s, "pad_length", 0) or 0),
                    int(getattr(s, "id", 0) or 0),
                )
            )
            prefix_to_series[prefix] = candidates[0]
            continue

        # Create a new series.
        desired = _suggest_series_name(prefix)
        name = _unique_series_name(existing_names, desired)
        s = InvoiceSeries(
            subject_id=subject_id,
            name=str(name),
            prefix=str(prefix),
            pad_length=int(spec.pad_length),
            last_counter=int(spec.max_counter),
        )
        db.add(s)
        db.flush()
        created_series += 1
        by_prefix.setdefault(prefix, []).append(s)
        prefix_to_series[prefix] = s

    # Update last_counter if needed.
    for prefix, spec in inferred.items():
        s = prefix_to_series.get(prefix)
        if s is None:
            continue
        if int(getattr(s, "last_counter", 0) or 0) < int(spec.max_counter):
            s.last_counter = int(spec.max_counter)
            db.add(s)
            updated_series += 1

    # Assign series_id on invoices (only when missing).
    # We do row-by-row to keep it DB-agnostic and safe for modest datasets.
    id_to_series_id: dict[int, int] = {}
    for inv_id, number, series_id in inv_rows:
        if series_id is not None:
            continue
        parts = split_invoice_number_prefix_counter(str(number or ""))
        if parts is None:
            continue
        prefix, _counter, _digits_len = parts
        s = prefix_to_series.get(prefix)
        if s is None or getattr(s, "id", None) is None:
            continue
        id_to_series_id[int(inv_id)] = int(s.id)

    if id_to_series_id:
        # Fetch invoice objects and update.
        for inv in db.scalars(select(Invoice).where(Invoice.id.in_(list(id_to_series_id.keys())))).all():
            sid = id_to_series_id.get(int(inv.id))
            if sid is None:
                continue
            inv.series_id = int(sid)
            db.add(inv)
            assigned_invoices += 1

    return {
        "created_series": int(created_series),
        "updated_series": int(updated_series),
        "assigned_invoices": int(assigned_invoices),
        "inferred_prefixes": len(inferred),
    }


# ---------------------------------------------------------------------------
# Import orchestrator
# ---------------------------------------------------------------------------


def process_import_run(
    db,
    *,
    run,
    subject_id: int,
    import_storage_root: Path,
) -> dict:
    """Process a single ImportRun.

    The function mutates DB state but does not commit; caller controls transaction.

    It auto-detects file type(s):

    - invoices XML (or ZIP with XML)
    - contacts CSV (or ZIP with CSV)
    - ZIP with both imports both (contacts first)
    """

    # Lazy imports to keep this module importable in DB-disabled environments.
    from sqlalchemy import select

    from fakturek.importing import ensure_import_map, lookup_imported_id
    from fakturek.models import Contact, Invoice, InvoiceItem, InvoiceParty, Subject, SubjectBankAccount
    from fakturek.public_links import ensure_invoice_public_link

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run nepatří k aktuálnímu subjektu")

    file_path = str(getattr(run, "file_path", "") or "").strip()
    if not file_path:
        raise ValueError("Import run nemá uložený soubor")

    filename, payload = _load_import_payload_bytes(run_file_path=file_path, storage_root=import_storage_root)
    assets = _payload_to_assets(filename, payload)
    config = _load_run_config(run)
    contact_csv_mapping = {
        str(key): str(value)
        for key, value in dict(config.get("contact_csv_mapping") or {}).items()
        if str(value or "").strip()
    }
    contact_conflict_mode = str(config.get("contact_conflict_mode") or "merge_existing").strip().lower() or "merge_existing"
    invoice_number_conflict_mode = (
        str(config.get("invoice_number_conflict_mode") or "skip").strip().lower() or "skip"
    )
    contact_conflict_mode = str(config.get("contact_conflict_mode") or "merge_existing").strip().lower() or "merge_existing"
    invoice_number_conflict_mode = (
        str(config.get("invoice_number_conflict_mode") or "skip").strip().lower() or "skip"
    )
    contact_conflict_mode = str(config.get("contact_conflict_mode") or "merge_existing").strip().lower() or "merge_existing"
    invoice_number_conflict_mode = (
        str(config.get("invoice_number_conflict_mode") or "skip").strip().lower() or "skip"
    )

    # Load seller (subject) once.
    seller_subject = db.scalar(select(Subject).where(Subject.id == int(subject_id)).limit(1))
    if seller_subject is None:
        raise ValueError("Subjekt neexistuje")

    source = str(getattr(run, "source", "fakturoid") or "fakturoid")
    subject_bank_accounts = list(
        db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
        ).all()
    )

    def _safe_normalize_iban(value: str | None) -> str:
        raw = re.sub(r"\s+", "", str(value or "")).upper()
        if not raw:
            return ""
        try:
            return normalize_iban(raw)
        except ValueError:
            return raw

    def _match_subject_bank_account(imported_account_number: str, imported_iban: str) -> SubjectBankAccount | None:
        target_iban = _safe_normalize_iban(imported_iban)
        target_number = normalize_spaces(imported_account_number)
        for account in subject_bank_accounts:
            account_iban = _safe_normalize_iban(getattr(account, "iban", None) or "")
            account_number = normalize_spaces(getattr(account, "account_number", None) or "")
            if target_iban and account_iban and target_iban == account_iban:
                return account
            if target_number and account_number and target_number == account_number:
                return account
        return None

    summary: dict = {
        "phase": 26,
        "source": source,
        "config": {
            "contact_csv_mapping": contact_csv_mapping,
            "contact_conflict_mode": contact_conflict_mode,
            "invoice_number_conflict_mode": invoice_number_conflict_mode,
        },
        "file": {
            "name": str(getattr(run, "file_name", "") or filename),
            "stored_name": filename,
            "sha256": str(getattr(run, "file_sha256", "") or ""),
            "size_bytes": int(getattr(run, "file_size_bytes", 0) or 0),
            "mime_type": str(getattr(run, "mime_type", "") or ""),
        },
        "detected": {
            "xml": assets.xml_name,
            "csv": assets.csv_name,
            "pdf_files": len(list(assets.pdf_files or [])),
        },
        "contacts": {
            "parsed": 0,
            "created": 0,
            "reused": 0,
            "skipped_existing": 0,
            "skipped_conflicts": 0,
            "errors": [],
        },
        "invoices": {
            "parsed": 0,
            "imported": 0,
            "skipped_existing": 0,
            "skipped_number_conflict": 0,
            "renumbered": 0,
            "public_links_created": 0,
            "public_links_backfilled": 0,
            "created_contacts": 0,
            "reused_contacts": 0,
            "warnings": [],
            "errors": [],
        },
        "series": None,
    }

    def _ensure_invoice_link(invoice_obj) -> None:
        had_link = bool(
            (getattr(invoice_obj, "public_token", None) or "").strip()
            and (getattr(seller_subject, "public_username", None) or "").strip()
        )
        ensure_invoice_public_link(db, invoice=invoice_obj, subject=seller_subject)
        has_link = bool(
            (getattr(invoice_obj, "public_token", None) or "").strip()
            and (getattr(seller_subject, "public_username", None) or "").strip()
        )
        if not had_link and has_link:
            if getattr(invoice_obj, "id", None) is None:
                summary["invoices"]["public_links_created"] += 1
            else:
                summary["invoices"]["public_links_backfilled"] += 1

    def _next_available_invoice_number(desired_number: str) -> str:
        base_number = str(desired_number or "").strip()[:50] or "IMP"
        parts = split_invoice_number_prefix_counter(base_number)
        if parts is not None:
            prefix, _counter, digits_len = parts
            existing_numbers = db.scalars(
                select(Invoice.number).where(Invoice.subject_id == int(subject_id))
            ).all()
            next_counter = 0
            for existing in existing_numbers:
                observed = split_invoice_number_prefix_counter(str(existing or ""))
                if observed is None:
                    continue
                observed_prefix, observed_counter, observed_digits = observed
                if observed_prefix == prefix and observed_digits == digits_len:
                    next_counter = max(int(next_counter), int(observed_counter))
            next_counter += 1
            return f"{prefix}{str(next_counter).zfill(digits_len)}"[:50]

        candidate = f"{base_number}-import"
        suffix = 1
        while db.scalar(
            select(Invoice.id)
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.number == candidate[:50])
            .limit(1)
        ) is not None:
            suffix += 1
            candidate = f"{base_number}-import-{suffix}"
        return candidate[:50]

    def _import_contacts(parsed_contacts: list[ParsedContact]) -> None:
        for c in parsed_contacts:
            try:
                ext_id = (c.external_id or "").strip() or None
                contact = None

                if ext_id:
                    already_id = lookup_imported_id(
                        db,
                        subject_id=int(subject_id),
                        source=str(source),
                        entity_type="contact",
                        external_id=str(ext_id),
                    )
                    if already_id is not None:
                        contact = db.scalar(
                            select(Contact)
                            .where(Contact.subject_id == int(subject_id))
                            .where(Contact.id == int(already_id))
                            .limit(1)
                        )

                # Try to reuse existing contact.
                if ext_id:
                    if contact is None:
                        contact = db.scalar(
                            select(Contact)
                            .where(Contact.subject_id == int(subject_id))
                            .where(Contact.external_source == str(source))
                            .where(Contact.external_id == str(ext_id))
                            .limit(1)
                        )

                if contact_conflict_mode != "create_new" and contact is None and c.ico:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.ico == str(c.ico))
                        .limit(1)
                    )

                if contact_conflict_mode != "create_new" and contact is None and c.email:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.email == str(c.email))
                        .limit(1)
                    )

                if contact_conflict_mode != "create_new" and contact is None and c.name:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.name == str(c.name))
                        .limit(1)
                    )

                if contact is None:
                    contact = Contact(
                        subject_id=int(subject_id),
                        name=str(c.name or "Bez názvu")[:255],
                        email=str(c.email or "")[:255] or None,
                        phone=str(c.phone or "")[:50] or None,
                        street=str(c.street or "")[:255] or None,
                        city=str(c.city or "")[:100] or None,
                        zip=str(c.zip or "")[:20] or None,
                        country=str((c.country or "CZ").upper())[:2] or None,
                        ico=str(c.ico or "")[:32] or None,
                        dic=str(c.dic or "")[:32] or None,
                        fixed_variable_symbol=(str(c.fixed_variable_symbol or "")[:10] or None),
                        external_source=str(source) if ext_id else None,
                        external_id=str(ext_id) if ext_id else None,
                    )
                    db.add(contact)
                    db.flush()
                    summary["contacts"]["created"] += 1
                else:
                    summary["contacts"]["reused"] += 1

                    if contact_conflict_mode == "skip_existing":
                        summary["contacts"]["skipped_existing"] += 1
                        if ext_id:
                            ensure_import_map(
                                db,
                                subject_id=int(subject_id),
                                source=str(source),
                                entity_type="contact",
                                external_id=str(ext_id),
                                internal_id=int(contact.id),
                            )
                        continue

                    # Best-effort fill missing fields (do not overwrite user edits).
                    changed = False
                    if (contact.email is None or not str(contact.email).strip()) and c.email:
                        contact.email = str(c.email)[:255]
                        changed = True
                    if (contact.phone is None or not str(contact.phone).strip()) and c.phone:
                        contact.phone = str(c.phone)[:50]
                        changed = True
                    if (contact.street is None or not str(contact.street).strip()) and c.street:
                        contact.street = str(c.street)[:255]
                        changed = True
                    if (contact.city is None or not str(contact.city).strip()) and c.city:
                        contact.city = str(c.city)[:100]
                        changed = True
                    if (contact.zip is None or not str(contact.zip).strip()) and c.zip:
                        contact.zip = str(c.zip)[:20]
                        changed = True
                    if (contact.country is None or not str(contact.country).strip()) and c.country:
                        contact.country = str(c.country).upper()[:2]
                        changed = True
                    if (contact.ico is None or not str(contact.ico).strip()) and c.ico:
                        contact.ico = str(c.ico)[:32]
                        changed = True
                    if (contact.dic is None or not str(contact.dic).strip()) and c.dic:
                        contact.dic = str(c.dic)[:32]
                        changed = True
                    if (contact.fixed_variable_symbol is None or not str(contact.fixed_variable_symbol).strip()) and c.fixed_variable_symbol:
                        contact.fixed_variable_symbol = str(c.fixed_variable_symbol)[:10]
                        changed = True
                    if (contact.external_id is None or not str(contact.external_id).strip()) and ext_id:
                        contact.external_source = str(source)
                        contact.external_id = str(ext_id)
                        changed = True

                    if changed:
                        db.add(contact)
                    else:
                        summary["contacts"]["skipped_existing"] += 1

                if ext_id:
                    ensure_import_map(
                        db,
                        subject_id=int(subject_id),
                        source=str(source),
                        entity_type="contact",
                        external_id=str(ext_id),
                        internal_id=int(contact.id),
                    )
            except Exception as exc:
                summary["contacts"]["errors"].append(
                    {
                        "external_id": c.external_id,
                        "name": c.name,
                        "error": str(exc),
                    }
                )

    # ------------------------------------------------------------------
    # Contacts import (CSV)
    # ------------------------------------------------------------------

    if assets.csv_bytes is not None:
        try:
            parsed_contacts = parse_contacts_csv_with_mapping(assets.csv_bytes, mapping=contact_csv_mapping)
            summary["contacts"]["parsed"] = len(parsed_contacts)
            _import_contacts(parsed_contacts)
        except Exception as exc:
            summary["contacts"]["errors"].append({"error": str(exc)})

    # ------------------------------------------------------------------
    # Invoices import (XML)
    # ------------------------------------------------------------------

    parsed: list[ParsedInvoice] = []
    invoice_input_kind: str | None = None

    if assets.xml_bytes is not None:
        try:
            invoice_parser, resolved_source = _select_invoice_parser(source, assets.xml_bytes)
            parsed = invoice_parser(assets.xml_bytes)
            summary["invoices"]["parsed"] = len(parsed)
            invoice_input_kind = resolved_source
            summary["detected"]["xml_format"] = resolved_source
        except Exception as exc:
            summary["invoices"]["errors"].append({"error": str(exc)})
    elif assets.pdf_files:
        invoice_input_kind = "pdf"
        for pdf_name, pdf_bytes in list(assets.pdf_files or []):
            try:
                parsed.append(parse_fakturoid_invoice_pdf(pdf_bytes, filename=pdf_name))
            except Exception as exc:
                summary["invoices"]["errors"].append({"file": pdf_name, "error": str(exc)})
        summary["invoices"]["parsed"] = len(parsed)

    if not parsed and assets.xml_bytes is not None and assets.csv_bytes is None:
        try:
            parsed_contacts_xml = parse_fakturoid_contacts_xml(assets.xml_bytes)
            if parsed_contacts_xml:
                summary["contacts"]["parsed"] = len(parsed_contacts_xml)
                _import_contacts(parsed_contacts_xml)
        except Exception as exc:
            summary["contacts"]["errors"].append({"error": str(exc)})

    if parsed:
        for inv in parsed:
            try:
                ext_id = (inv.external_id or "").strip()

                # ImportMap check: invoice already imported.
                already_invoice_id = lookup_imported_id(
                    db,
                    subject_id=int(subject_id),
                    source=str(source),
                    entity_type="invoice",
                    external_id=str(ext_id),
                )
                if already_invoice_id is not None:
                    summary["invoices"]["skipped_existing"] += 1
                    try:
                        existing_invoice = db.scalar(
                            select(Invoice)
                            .where(Invoice.id == int(already_invoice_id))
                            .where(Invoice.subject_id == int(subject_id))
                            .limit(1)
                        )
                        if existing_invoice is not None:
                            _ensure_invoice_link(existing_invoice)
                    except Exception as exc:
                        summary["invoices"]["warnings"].append(
                            {
                                "invoice": str(inv.number or ""),
                                "reason": f"public link backfill failed for existing invoice: {exc}",
                                "external_id": ext_id,
                            }
                        )
                    continue

                number = (inv.number or "").strip()
                if not number:
                    number = f"IMP-{ext_id}"[:50]

                # Subject-level uniqueness for invoice numbers.
                existing_by_number = db.scalar(
                    select(Invoice)
                    .where(Invoice.subject_id == int(subject_id))
                    .where(Invoice.number == str(number))
                    .limit(1)
                )
                if existing_by_number is not None:
                    if invoice_number_conflict_mode == "renumber":
                        original_number = number
                        number = _next_available_invoice_number(number)
                        summary["invoices"]["renumbered"] += 1
                        summary["invoices"]["warnings"].append(
                            {
                                "invoice": original_number,
                                "reason": f"invoice number already exists; imported as {number}",
                                "external_id": ext_id,
                            }
                        )
                    else:
                        summary["invoices"]["skipped_number_conflict"] += 1
                        try:
                            _ensure_invoice_link(existing_by_number)
                        except Exception as exc:
                            summary["invoices"]["warnings"].append(
                                {
                                    "invoice": number,
                                    "reason": f"public link backfill failed for conflicting invoice: {exc}",
                                    "external_id": ext_id,
                                }
                            )
                        summary["invoices"]["warnings"].append(
                            {
                                "invoice": number,
                                "reason": "invoice number already exists",
                                "external_id": ext_id,
                            }
                        )
                        continue

                # Ensure / reuse buyer contact.
                buyer = inv.buyer
                buyer_name = (buyer.name or "").strip() or "Bez názvu"

                contact = None
                if inv.buyer_external_id:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.external_source == str(source))
                        .where(Contact.external_id == str(inv.buyer_external_id))
                        .limit(1)
                    )
                if contact is None and buyer.ico:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.ico == str(buyer.ico))
                        .limit(1)
                    )
                if contact is None:
                    contact = db.scalar(
                        select(Contact)
                        .where(Contact.subject_id == int(subject_id))
                        .where(Contact.name == str(buyer_name))
                        .limit(1)
                    )

                if contact is None:
                    contact = Contact(
                        subject_id=int(subject_id),
                        name=str(buyer_name)[:255],
                        email=buyer.email or None,
                        phone=buyer.phone or None,
                        street=buyer.street or None,
                        city=buyer.city or None,
                        zip=buyer.zip or None,
                        country=buyer.country or None,
                        ico=buyer.ico or None,
                        dic=buyer.dic or None,
                        external_source=str(source) if inv.buyer_external_id else None,
                        external_id=str(inv.buyer_external_id) if inv.buyer_external_id else None,
                    )
                    db.add(contact)
                    db.flush()
                    summary["invoices"]["created_contacts"] += 1
                else:
                    summary["invoices"]["reused_contacts"] += 1
                    # Best-effort fill missing fields (do not overwrite user edits).
                    changed = False
                    if (contact.email is None or not str(contact.email).strip()) and buyer.email:
                        contact.email = buyer.email
                        changed = True
                    if (contact.phone is None or not str(contact.phone).strip()) and buyer.phone:
                        contact.phone = buyer.phone
                        changed = True
                    if (contact.street is None or not str(contact.street).strip()) and buyer.street:
                        contact.street = buyer.street
                        changed = True
                    if (contact.city is None or not str(contact.city).strip()) and buyer.city:
                        contact.city = buyer.city
                        changed = True
                    if (contact.zip is None or not str(contact.zip).strip()) and buyer.zip:
                        contact.zip = buyer.zip
                        changed = True
                    if (contact.country is None or not str(contact.country).strip()) and buyer.country:
                        contact.country = buyer.country
                        changed = True
                    if (contact.ico is None or not str(contact.ico).strip()) and buyer.ico:
                        contact.ico = buyer.ico
                        changed = True
                    if (contact.dic is None or not str(contact.dic).strip()) and buyer.dic:
                        contact.dic = buyer.dic
                        changed = True
                    if changed:
                        db.add(contact)

                if inv.buyer_external_id:
                    ensure_import_map(
                        db,
                        subject_id=int(subject_id),
                        source=str(source),
                        entity_type="contact",
                        external_id=str(inv.buyer_external_id),
                        internal_id=int(contact.id),
                    )

                # Compute totals and rounding adjustment.
                items_total = sum(int(li.total_cents) for li in inv.lines)
                rounding_adj = 0
                if inv.total_cents is not None:
                    rounding_adj = int(inv.total_cents) - int(items_total)
                invoice_total = int(items_total) + int(rounding_adj)

                mapped_status = _map_invoice_status(inv.status)
                issue_date = inv.issue_date or date.today()
                due_date = inv.due_date or issue_date

                # Build invoice.
                footer_mode = _default_invoice_footer_mode_for_subject(seller_subject)

                invoice = Invoice(
                    subject_id=int(subject_id),
                    contact_id=int(contact.id),
                    number=str(number)[:50],
                    variable_symbol=(digits_only(getattr(inv, "variable_symbol", "") or "")[:10] or None),
                    status=str(mapped_status),
                    issue_date=issue_date,
                    taxable_supply_date=getattr(inv, "taxable_supply_date", None) or issue_date,
                    due_date=due_date,
                    currency=str(inv.currency or seller_subject.default_currency or "CZK")[:3],
                    notes=str(inv.note or ""),
                    internal_notes=str(inv.private_note or ""),
                    payment_method=str(getattr(inv, "payment_method", "") or "bank_transfer"),
                    total_cents=int(invoice_total),
                    rounding_adjustment_cents=int(rounding_adj),
                    buyer_name_cache=str(contact.name or ""),
                    buyer_registration_no_cache=str(contact.ico or ""),
                    footer_mode=footer_mode,
                    footer_text=_invoice_footer_text_for_mode(
                        footer_mode,
                        subject=seller_subject,
                    ),
                )

                imported_bank_account = (getattr(inv, "bank_account", "") or "").strip()
                imported_iban = (getattr(inv, "iban", "") or "").strip()
                imported_bic = (getattr(inv, "bic", "") or "").strip()
                if imported_bank_account or imported_iban:
                    linked_bank_account = _match_subject_bank_account(imported_bank_account, imported_iban)
                    try:
                        if linked_bank_account is not None:
                            invoice.bank_account_id = int(linked_bank_account.id)
                            payload = resolve_bank_account(
                                account_number=(getattr(linked_bank_account, "account_number", "") or ""),
                                iban=(getattr(linked_bank_account, "iban", None) or imported_iban),
                                bic=(getattr(linked_bank_account, "bic", None) or imported_bic),
                                country=(getattr(linked_bank_account, "country", None) or (imported_iban[:2] if len(imported_iban) >= 2 and imported_iban[:2].isalpha() else str(getattr(seller_subject, "country", None) or "CZ"))),
                                label=(getattr(linked_bank_account, "label", None) or imported_bank_account or imported_iban or "Importovaný účet"),
                            )
                        else:
                            payload = resolve_bank_account(
                                account_number=imported_bank_account,
                                iban=imported_iban,
                                bic=imported_bic,
                                country=(imported_iban[:2] if len(imported_iban) >= 2 and imported_iban[:2].isalpha() else str(getattr(seller_subject, "country", None) or "CZ")),
                                label=imported_bank_account or imported_iban or "Importovaný účet",
                            )
                        invoice.bank_account_label = payload.label or None
                        invoice.bank_account_number = payload.number or None
                        invoice.bank_account_iban = payload.iban or None
                        invoice.bank_account_bic = payload.bic or None
                        invoice.bank_account_country = payload.country or None
                    except ValueError:
                        invoice.bank_account_label = imported_bank_account or imported_iban or "Importovaný účet"
                        invoice.bank_account_number = imported_bank_account or None
                        invoice.bank_account_iban = imported_iban or None
                        invoice.bank_account_bic = imported_bic or None
                        invoice.bank_account_country = (
                            imported_iban[:2].upper()
                            if len(imported_iban) >= 2 and imported_iban[:2].isalpha()
                            else str(getattr(seller_subject, "country", None) or "CZ")
                        )
                _ensure_invoice_link(invoice)

                # Status timestamps (best-effort).
                if mapped_status in {"issued", "sent", "paid"}:
                    invoice.issued_at = datetime.combine(issue_date, datetime.min.time())
                if mapped_status in {"sent", "paid"} and inv.sent_at is not None:
                    invoice.sent_at = inv.sent_at
                if mapped_status == "paid" and inv.paid_on is not None:
                    invoice.paid_on = inv.paid_on

                # Preserve unknown Fakturoid statuses.
                if (inv.status or "").strip().lower() not in {"open", "sent", "overdue", "paid", "draft", "concept"}:
                    extra = f"Imported status: {inv.status}".strip()
                    if extra:
                        invoice.internal_notes = (invoice.internal_notes or "").strip()
                        invoice.internal_notes = (
                            (invoice.internal_notes + "\n" + extra).strip() if invoice.internal_notes else extra
                        )

                db.add(invoice)
                db.flush()

                # Items.
                for idx, li in enumerate(inv.lines):
                    item = InvoiceItem(
                        invoice_id=int(invoice.id),
                        description=str(li.description)[:255],
                        quantity=li.quantity,
                        unit=str(getattr(li, "unit", "") or "")[:32],
                        unit_price_cents=int(li.unit_price_cents),
                        vat_rate=li.vat_rate,
                        line_net_cents=int(li.net_cents),
                        line_vat_cents=int(li.vat_cents),
                        line_total_cents=int(li.total_cents),
                        sort_order=int(idx),
                    )
                    db.add(item)

                # Parties snapshots.
                buyer_party = InvoiceParty(
                    invoice_id=int(invoice.id),
                    role="buyer",
                    name=str(buyer_name)[:255],
                    email=str(buyer.email or "")[:255],
                    phone=str(buyer.phone or "")[:50],
                    street=str(buyer.street or "")[:255],
                    city=str(buyer.city or "")[:100],
                    zip=str(buyer.zip or "")[:20],
                    country=str((buyer.country or "CZ").upper())[:2],
                    ico=str(buyer.ico or "")[:32],
                    dic=str(buyer.dic or "")[:32],
                )

                seller_snapshot = inv.seller or seller_subject
                seller_party = InvoiceParty(
                    invoice_id=int(invoice.id),
                    role="seller",
                    name=str(getattr(seller_snapshot, "name", "") or "")[:255],
                    email=str(getattr(seller_snapshot, "email", "") or "")[:255],
                    phone=str(getattr(seller_snapshot, "phone", "") or "")[:50],
                    street=str(getattr(seller_snapshot, "street", "") or "")[:255],
                    city=str(getattr(seller_snapshot, "city", "") or "")[:100],
                    zip=str(getattr(seller_snapshot, "zip", "") or "")[:20],
                    country=str((getattr(seller_snapshot, "country", "CZ") or "CZ").upper())[:2],
                    ico=str(getattr(seller_snapshot, "ico", "") or "")[:32],
                    dic=str(getattr(seller_snapshot, "dic", "") or "")[:32],
                )

                db.add(buyer_party)
                db.add(seller_party)

                db.flush()

                ensure_import_map(
                    db,
                    subject_id=int(subject_id),
                    source=str(source),
                    entity_type="invoice",
                    external_id=str(ext_id),
                    internal_id=int(invoice.id),
                )

                summary["invoices"]["imported"] += 1
            except Exception as exc:
                summary["invoices"]["errors"].append(
                    {
                        "external_id": getattr(inv, "external_id", None),
                        "number": getattr(inv, "number", None),
                        "source": invoice_input_kind,
                        "error": str(exc),
                    }
                )
                # Continue with the next invoice.
                continue

        # Reconcile invoice series after invoice import (best effort).
        try:
            summary["series"] = reconcile_invoice_series_for_subject(db, subject_id=int(subject_id))
        except Exception as exc:
            summary["series"] = {"error": str(exc)}

    # Add a compact note for UI.
    parts = []
    if assets.csv_bytes is not None:
        parts.append(
            f"contacts: +{summary['contacts']['created']} (reused {summary['contacts']['reused']}, skipped {summary['contacts']['skipped_existing']})"
        )
    if assets.xml_bytes is not None or assets.pdf_files:
        parts.append(
            f"invoices: +{summary['invoices']['imported']} (skipped {summary['invoices']['skipped_existing']} existing, {summary['invoices']['skipped_number_conflict']} conflicts)"
        )
        if int(summary["invoices"].get("renumbered", 0) or 0):
            parts.append(f"renumbered: {summary['invoices'].get('renumbered', 0)}")
        public_created = int(summary["invoices"].get("public_links_created", 0) or 0)
        public_backfilled = int(summary["invoices"].get("public_links_backfilled", 0) or 0)
        if public_created or public_backfilled:
            parts.append(f"public links: +{public_created} created, +{public_backfilled} backfilled")
    if summary.get("series") and isinstance(summary.get("series"), dict) and not summary["series"].get("error"):
        parts.append(
            f"series: +{summary['series'].get('created_series', 0)} created, {summary['series'].get('updated_series', 0)} bumped"
        )

    summary["note"] = "; ".join(parts) if parts else "No supported assets detected."

    return summary


def preview_import_run(
    db,
    *,
    run,
    subject_id: int,
    import_storage_root: Path,
) -> dict:
    from sqlalchemy import select

    from fakturek.importing import lookup_imported_id
    from fakturek.models import Contact, Invoice, Subject, SubjectBankAccount

    if int(getattr(run, "subject_id", 0) or 0) != int(subject_id):
        raise ValueError("Import run nepatří k aktuálnímu subjektu")

    file_path = str(getattr(run, "file_path", "") or "").strip()
    if not file_path:
        raise ValueError("Import run nemá uložený soubor")

    filename, payload = _load_import_payload_bytes(run_file_path=file_path, storage_root=import_storage_root)
    assets = _payload_to_assets(filename, payload)
    source = str(getattr(run, "source", "fakturoid") or "fakturoid")
    config = _load_run_config(run)
    contact_csv_mapping = {
        str(key): str(value)
        for key, value in dict(config.get("contact_csv_mapping") or {}).items()
        if str(value or "").strip()
    }
    contact_conflict_mode = str(config.get("contact_conflict_mode") or "merge_existing").strip().lower() or "merge_existing"
    invoice_number_conflict_mode = (
        str(config.get("invoice_number_conflict_mode") or "skip").strip().lower() or "skip"
    )

    seller_subject = db.scalar(select(Subject).where(Subject.id == int(subject_id)).limit(1))
    if seller_subject is None:
        raise ValueError("Subjekt neexistuje")

    subject_bank_accounts = list(
        db.scalars(
            select(SubjectBankAccount)
            .where(SubjectBankAccount.subject_id == int(subject_id))
            .order_by(SubjectBankAccount.is_default.desc(), SubjectBankAccount.sort_order.asc(), SubjectBankAccount.id.asc())
        ).all()
    )

    def _match_existing_contact(parsed_contact: ParsedContact) -> tuple[object | None, str | None]:
        ext_id = (parsed_contact.external_id or "").strip()
        contact = None
        reason = None
        if ext_id:
            already_id = lookup_imported_id(
                db,
                subject_id=int(subject_id),
                source=str(source),
                entity_type="contact",
                external_id=str(ext_id),
            )
            if already_id is not None:
                contact = db.scalar(
                    select(Contact)
                    .where(Contact.subject_id == int(subject_id))
                    .where(Contact.id == int(already_id))
                    .limit(1)
                )
                if contact is not None:
                    return contact, "import_map"
        if parsed_contact.ico:
            contact = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(Contact.ico == str(parsed_contact.ico))
                .limit(1)
            )
            if contact is not None:
                return contact, "ico"
        if parsed_contact.email:
            contact = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(Contact.email == str(parsed_contact.email))
                .limit(1)
            )
            if contact is not None:
                return contact, "email"
        if parsed_contact.name:
            contact = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(Contact.name == str(parsed_contact.name))
                .limit(1)
            )
            if contact is not None:
                return contact, "name"
        return None, reason

    def _match_existing_contact_for_buyer(inv: ParsedInvoice) -> tuple[object | None, str | None]:
        buyer = inv.buyer
        ext_id = str(inv.buyer_external_id or "").strip()
        if ext_id:
            contact = db.scalar(
                select(Contact)
                .where(Contact.subject_id == int(subject_id))
                .where(Contact.external_source == str(source))
                .where(Contact.external_id == ext_id)
                .limit(1)
            )
            if contact is not None:
                return contact, "buyer_external_id"
        parsed_contact = ParsedContact(
            external_id=ext_id or None,
            name=str(buyer.name or "").strip(),
            email=str(buyer.email or "").strip(),
            phone=str(buyer.phone or "").strip(),
            street=str(buyer.street or "").strip(),
            city=str(buyer.city or "").strip(),
            zip=str(buyer.zip or "").strip(),
            country=str(buyer.country or "CZ").strip().upper() or "CZ",
            ico=str(buyer.ico or "").strip(),
            dic=str(buyer.dic or "").strip(),
            fixed_variable_symbol="",
        )
        return _match_existing_contact(parsed_contact)

    preview: dict[str, object] = {
        "source": source,
        "config": {
            "contact_csv_mapping": contact_csv_mapping,
            "contact_conflict_mode": contact_conflict_mode,
            "invoice_number_conflict_mode": invoice_number_conflict_mode,
        },
        "file": {
            "name": str(getattr(run, "file_name", "") or filename),
            "stored_name": filename,
            "size_bytes": int(getattr(run, "file_size_bytes", 0) or 0),
        },
        "detected": {
            "xml": assets.xml_name,
            "csv": assets.csv_name,
            "pdf_files": len(list(assets.pdf_files or [])),
        },
        "contacts": {
            "parsed": 0,
            "will_create": 0,
            "will_reuse": 0,
            "will_skip": 0,
            "match_reasons": {},
            "headers": [],
            "inferred_mapping": {},
            "sample_rows": [],
            "sample_contacts": [],
            "errors": [],
        },
        "invoices": {
            "parsed": 0,
            "will_import": 0,
            "already_imported": 0,
            "number_conflicts": 0,
            "will_renumber": 0,
            "bank_account_matches": 0,
            "bank_account_missing": 0,
            "will_create_contacts": 0,
            "will_reuse_contacts": 0,
            "warnings": [],
            "errors": [],
        },
    }

    if assets.csv_bytes is not None:
        try:
            csv_meta = inspect_contacts_csv(assets.csv_bytes)
            preview["contacts"]["headers"] = list(csv_meta.get("headers") or [])
            preview["contacts"]["inferred_mapping"] = dict(csv_meta.get("inferred_mapping") or {})
            preview["contacts"]["sample_rows"] = list(csv_meta.get("sample_rows") or [])
            parsed_contacts = parse_contacts_csv_with_mapping(assets.csv_bytes, mapping=contact_csv_mapping)
            preview["contacts"]["parsed"] = len(parsed_contacts)
            preview["contacts"]["sample_contacts"] = [
                {
                    "name": str(c.name or ""),
                    "email": str(c.email or ""),
                    "ico": str(c.ico or ""),
                    "city": str(c.city or ""),
                }
                for c in parsed_contacts[:5]
            ]
            match_reasons: dict[str, int] = {}
            for c in parsed_contacts:
                existing, reason = _match_existing_contact(c)
                if existing is None:
                    preview["contacts"]["will_create"] += 1
                else:
                    preview["contacts"]["will_reuse"] += 1
                    if contact_conflict_mode == "skip_existing":
                        preview["contacts"]["will_skip"] += 1
                    if reason:
                        match_reasons[reason] = int(match_reasons.get(reason, 0) or 0) + 1
            preview["contacts"]["match_reasons"] = match_reasons
        except Exception as exc:
            preview["contacts"]["errors"].append({"error": str(exc)})

    parsed_invoices: list[ParsedInvoice] = []
    if assets.xml_bytes is not None:
        try:
            invoice_parser, resolved_source = _select_invoice_parser(source, assets.xml_bytes)
            parsed_invoices = invoice_parser(assets.xml_bytes)
            preview["detected"]["xml_format"] = resolved_source
        except Exception as exc:
            preview["invoices"]["errors"].append({"error": str(exc)})
    elif assets.pdf_files:
        for pdf_name, pdf_bytes in list(assets.pdf_files or []):
            try:
                parsed_invoices.append(parse_fakturoid_invoice_pdf(pdf_bytes, filename=pdf_name))
            except Exception as exc:
                preview["invoices"]["errors"].append({"file": pdf_name, "error": str(exc)})

    preview["invoices"]["parsed"] = len(parsed_invoices)

    account_numbers = {
        normalize_spaces(str(getattr(account, "account_number", None) or ""))
        for account in subject_bank_accounts
        if str(getattr(account, "account_number", None) or "").strip()
    }
    account_ibans = {
        re.sub(r"\s+", "", str(getattr(account, "iban", None) or "")).upper()
        for account in subject_bank_accounts
        if str(getattr(account, "iban", None) or "").strip()
    }

    for inv in parsed_invoices:
        ext_id = str(getattr(inv, "external_id", "") or "").strip()
        if ext_id:
            already_id = lookup_imported_id(
                db,
                subject_id=int(subject_id),
                source=str(source),
                entity_type="invoice",
                external_id=ext_id,
            )
            if already_id is not None:
                preview["invoices"]["already_imported"] += 1
                continue

        number = (str(getattr(inv, "number", "") or "").strip() or f"IMP-{ext_id}")[:50]
        existing_by_number = db.scalar(
            select(Invoice)
            .where(Invoice.subject_id == int(subject_id))
            .where(Invoice.number == number)
            .limit(1)
        )
        if existing_by_number is not None:
            preview["invoices"]["number_conflicts"] += 1
            if invoice_number_conflict_mode == "renumber":
                preview["invoices"]["will_renumber"] += 1
                preview["invoices"]["will_import"] += 1
                preview["invoices"]["warnings"].append(
                    {"invoice": number, "reason": "invoice number already exists; import dostane nové číslo"}
                )
            else:
                preview["invoices"]["warnings"].append(
                    {"invoice": number, "reason": "invoice number already exists"}
                )
                continue

        if existing_by_number is None:
            preview["invoices"]["will_import"] += 1
        contact, _reason = _match_existing_contact_for_buyer(inv)
        if contact is None:
            preview["invoices"]["will_create_contacts"] += 1
        else:
            preview["invoices"]["will_reuse_contacts"] += 1

        imported_number = normalize_spaces(str(getattr(inv, "bank_account", "") or ""))
        imported_iban = re.sub(r"\s+", "", str(getattr(inv, "iban", "") or "")).upper()
        if imported_number or imported_iban:
            if (imported_number and imported_number in account_numbers) or (imported_iban and imported_iban in account_ibans):
                preview["invoices"]["bank_account_matches"] += 1
            else:
                preview["invoices"]["bank_account_missing"] += 1

    preview["ready_note"] = (
        f"Kontakty: {preview['contacts']['parsed']} nalezeno, "
        f"{preview['contacts']['will_create']} nových. "
        f"Faktury: {preview['invoices']['parsed']} nalezeno, "
        f"{preview['invoices']['will_import']} připraveno k importu."
    )
    return preview


def summary_to_json(summary: dict) -> str:
    try:
        return json.dumps(summary, ensure_ascii=False)
    except Exception:
        return json.dumps({"phase": 26, "error": "failed to serialize summary"})

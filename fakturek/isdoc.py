from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import NAMESPACE_URL, uuid5
import xml.etree.ElementTree as ET


NS = "http://isdoc.cz/namespace/2013"
ISDOC_VERSION = "6.0.2"
ET.register_namespace("", NS)


def _text(value: object | None, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _date_text(value: object | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _decimal(value: object | None, *, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


def _money_decimal(cents: object | None) -> str:
    value = (Decimal(int(cents or 0)) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(value, "f")


def _amount_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _append(parent: ET.Element, tag: str, value: object | None, attrs: dict[str, str] | None = None) -> ET.Element:
    node = ET.SubElement(parent, ET.QName(NS, tag), attrs or {})
    node.text = _text(value)
    return node


def _country_name(country_code: str) -> str:
    return {
        "CZ": "Česká republika",
        "SK": "Slovensko",
    }.get(country_code.upper(), country_code.upper())


def _document_type_code(value: object | None) -> str:
    normalized = _text(value or "invoice").lower()
    if normalized == "credit_note":
        return "2"
    if normalized == "proforma":
        return "4"
    return "1"


def _append_party(parent: ET.Element, tag: str, party: dict[str, object]) -> None:
    wrapper = ET.SubElement(parent, ET.QName(NS, tag))
    party_el = ET.SubElement(wrapper, ET.QName(NS, "Party"))

    identification = ET.SubElement(party_el, ET.QName(NS, "PartyIdentification"))
    _append(identification, "ID", _text(party.get("ico"), fallback="0"))

    party_name = ET.SubElement(party_el, ET.QName(NS, "PartyName"))
    _append(party_name, "Name", _text(party.get("name"), fallback="-"))

    address = ET.SubElement(party_el, ET.QName(NS, "PostalAddress"))
    _append(address, "StreetName", _text(party.get("street"), fallback="-"))
    _append(address, "BuildingNumber", "")
    _append(address, "CityName", _text(party.get("city"), fallback="-"))
    _append(address, "PostalZone", _text(party.get("zip"), fallback="-"))
    country_code = _text(party.get("country"), fallback="CZ").upper()
    country = ET.SubElement(address, ET.QName(NS, "Country"))
    _append(country, "IdentificationCode", country_code)
    _append(country, "Name", _country_name(country_code))

    if _text(party.get("dic")):
        tax_scheme = ET.SubElement(party_el, ET.QName(NS, "PartyTaxScheme"))
        _append(tax_scheme, "CompanyID", party.get("dic"))
        _append(tax_scheme, "TaxScheme", "VAT")

    if _text(party.get("name")) or _text(party.get("email")) or _text(party.get("phone")):
        contact = ET.SubElement(party_el, ET.QName(NS, "Contact"))
        _append(contact, "Name", party.get("name"))
        if _text(party.get("phone")):
            _append(contact, "Telephone", party.get("phone"))
        if _text(party.get("email")):
            _append(contact, "ElectronicMail", party.get("email"))


def _invoice_uuid(invoice: Any) -> str:
    invoice_id = int(getattr(invoice, "id", 0) or 0)
    invoice_number = _text(getattr(invoice, "number", None))
    return str(uuid5(NAMESPACE_URL, f"fakturek.cz/invoices/{invoice_id}/{invoice_number}"))


def _line_unit_price_tax_inclusive_cents(item: Any) -> int:
    quantity = _decimal(getattr(item, "quantity", None), default="1")
    if quantity == 0:
        return int(getattr(item, "unit_price_cents", 0) or 0)
    return int((Decimal(int(getattr(item, "line_total_cents", 0) or 0)) / quantity).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _tax_breakdown(items: list[Any]) -> dict[str, dict[str, int]]:
    breakdown: dict[str, dict[str, int]] = {}
    for item in items:
        rate = format(_decimal(getattr(item, "vat_rate", None)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        group = breakdown.setdefault(rate, {"net": 0, "vat": 0, "total": 0})
        group["net"] += int(getattr(item, "line_net_cents", 0) or 0)
        group["vat"] += int(getattr(item, "line_vat_cents", 0) or 0)
        group["total"] += int(getattr(item, "line_total_cents", 0) or 0)
    return breakdown


def build_isdoc_bytes(*, invoice: Any, ctx: dict[str, Any]) -> bytes:
    root = ET.Element(ET.QName(NS, "Invoice"), {"version": ISDOC_VERSION})
    seller = {k: _text(v) for k, v in dict(ctx.get("seller") or {}).items()}
    buyer = {k: _text(v) for k, v in dict(ctx.get("buyer") or {}).items()}
    payment_account = ctx.get("payment_account")
    items = list(ctx.get("items") or [])
    variable_symbol = _text(ctx.get("variable_symbol") or getattr(invoice, "variable_symbol", None) or getattr(invoice, "number", None))
    document_type = _text(ctx.get("document_type") or getattr(invoice, "document_type", None) or "invoice")
    currency = _text(getattr(invoice, "currency", None), fallback="CZK").upper()
    issue_date = _date_text(getattr(invoice, "issue_date", None))
    tax_point_date = _date_text(getattr(invoice, "taxable_supply_date", None)) or issue_date
    due_date = _date_text(getattr(invoice, "due_date", None)) or issue_date

    _append(root, "DocumentType", _document_type_code(document_type))
    _append(root, "ID", getattr(invoice, "number", None))
    _append(root, "UUID", _invoice_uuid(invoice))
    _append(root, "IssuingSystem", "Fakturek.cz")
    _append(root, "IssueDate", issue_date)
    _append(root, "TaxPointDate", tax_point_date)
    _append(root, "VATApplicable", "true" if any(int(getattr(item, "line_vat_cents", 0) or 0) for item in items) else "false")
    _append(root, "ElectronicPossibilityAgreementReference", "Fakturek.cz")
    if _text(getattr(invoice, "notes", None)):
        _append(root, "Note", getattr(invoice, "notes", None))
    _append(root, "LocalCurrencyCode", currency)
    _append(root, "CurrRate", "1")
    _append(root, "RefCurrRate", "1")

    _append_party(root, "AccountingSupplierParty", seller)
    _append_party(root, "AccountingCustomerParty", buyer)

    invoice_lines = ET.SubElement(root, ET.QName(NS, "InvoiceLines"))
    for index, item in enumerate(items, start=1):
        line = ET.SubElement(invoice_lines, ET.QName(NS, "InvoiceLine"))
        quantity = _decimal(getattr(item, "quantity", None), default="1")
        _append(line, "ID", index)
        _append(line, "InvoicedQuantity", format(quantity, "f"), {"unitCode": _text(getattr(item, "unit", None), fallback="ks")})
        _append(line, "LineExtensionAmount", _money_decimal(getattr(item, "line_net_cents", None)))
        _append(line, "LineExtensionAmountTaxInclusive", _money_decimal(getattr(item, "line_total_cents", None)))
        _append(line, "LineExtensionTaxAmount", _money_decimal(getattr(item, "line_vat_cents", None)))
        _append(line, "UnitPrice", _money_decimal(getattr(item, "unit_price_cents", None)))
        _append(line, "UnitPriceTaxInclusive", _money_decimal(_line_unit_price_tax_inclusive_cents(item)))
        tax_category = ET.SubElement(line, ET.QName(NS, "ClassifiedTaxCategory"))
        _append(tax_category, "Percent", _amount_decimal(_decimal(getattr(item, "vat_rate", None))))
        _append(tax_category, "VATCalculationMethod", "0")
        _append(tax_category, "VATApplicable", "true" if int(getattr(item, "line_vat_cents", 0) or 0) else "false")
        item_el = ET.SubElement(line, ET.QName(NS, "Item"))
        _append(item_el, "Description", _text(getattr(item, "description", None), fallback="-"))

    breakdown = _tax_breakdown(items)
    tax_total = ET.SubElement(root, ET.QName(NS, "TaxTotal"))
    for rate, group in sorted(breakdown.items(), key=lambda entry: Decimal(entry[0])):
        subtotal = ET.SubElement(tax_total, ET.QName(NS, "TaxSubTotal"))
        _append(subtotal, "TaxableAmount", _money_decimal(group["net"]))
        _append(subtotal, "TaxAmount", _money_decimal(group["vat"]))
        _append(subtotal, "TaxInclusiveAmount", _money_decimal(group["total"]))
        _append(subtotal, "AlreadyClaimedTaxableAmount", "0.00")
        _append(subtotal, "AlreadyClaimedTaxAmount", "0.00")
        _append(subtotal, "AlreadyClaimedTaxInclusiveAmount", "0.00")
        _append(subtotal, "DifferenceTaxableAmount", _money_decimal(group["net"]))
        _append(subtotal, "DifferenceTaxAmount", _money_decimal(group["vat"]))
        _append(subtotal, "DifferenceTaxInclusiveAmount", _money_decimal(group["total"]))
        tax_category = ET.SubElement(subtotal, ET.QName(NS, "TaxCategory"))
        _append(tax_category, "Percent", rate)
        _append(tax_category, "TaxScheme", "VAT")
        _append(tax_category, "VATApplicable", "true" if group["vat"] else "false")
    _append(tax_total, "TaxAmount", _money_decimal(sum(group["vat"] for group in breakdown.values())))

    net_total_cents = sum(group["net"] for group in breakdown.values())
    gross_total_cents = sum(group["total"] for group in breakdown.values())
    discount_cents = int(ctx.get("discount_cents") or 0)
    rounding_cents = int(ctx.get("rounding_adjustment_cents") or 0)
    payable_cents = int(getattr(invoice, "total_cents", 0) or 0)
    monetary_total = ET.SubElement(root, ET.QName(NS, "LegalMonetaryTotal"))
    _append(monetary_total, "TaxExclusiveAmount", _money_decimal(net_total_cents))
    _append(monetary_total, "TaxInclusiveAmount", _money_decimal(gross_total_cents))
    _append(monetary_total, "AlreadyClaimedTaxExclusiveAmount", "0.00")
    _append(monetary_total, "AlreadyClaimedTaxInclusiveAmount", "0.00")
    _append(monetary_total, "DifferenceTaxExclusiveAmount", _money_decimal(net_total_cents - discount_cents))
    _append(monetary_total, "DifferenceTaxInclusiveAmount", _money_decimal(payable_cents - rounding_cents))
    _append(monetary_total, "PayableRoundingAmount", _money_decimal(rounding_cents))
    _append(monetary_total, "PaidDepositsAmount", "0.00")
    _append(monetary_total, "PayableAmount", _money_decimal(payable_cents))

    if payment_account is not None:
        payment_means = ET.SubElement(root, ET.QName(NS, "PaymentMeans"))
        payment = ET.SubElement(payment_means, ET.QName(NS, "Payment"), {"partialPayment": "false"})
        _append(payment, "PaidAmount", _money_decimal(payable_cents))
        _append(payment, "PaymentMeansCode", "42")
        details = ET.SubElement(payment, ET.QName(NS, "Details"))
        _append(details, "PaymentDueDate", due_date)
        account_number = _text(getattr(payment_account, "number", None))
        if "/" in account_number:
            account_id, bank_code = account_number.split("/", 1)
        else:
            account_id, bank_code = account_number, ""
        _append(details, "ID", account_id)
        _append(details, "BankCode", bank_code)
        _append(details, "Name", _text(getattr(payment_account, "label", None), fallback=_text(seller.get("name"), fallback="-")))
        _append(details, "IBAN", _text(getattr(payment_account, "iban", None)))
        _append(details, "BIC", _text(getattr(payment_account, "bic", None)))
        if variable_symbol:
            _append(details, "VariableSymbol", variable_symbol)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

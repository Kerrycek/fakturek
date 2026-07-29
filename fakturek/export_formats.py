from __future__ import annotations
from fakturek.time_utils import utc_now

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import xml.etree.ElementTree as ET


def _money_decimal_from_cents(value: object | None) -> Decimal:
    cents = int(value or 0)
    return (Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_to_str(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _value_text(value: object | None) -> str:
    return str(value or "").strip()


def _date_text(value: object | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _value_text(value)
    return raw


def _safe_country(value: object | None) -> str:
    raw = _value_text(value).upper()
    return raw[:2] if raw else ""


def _append_text(parent: ET.Element, tag: str, value: object | None) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.text = _value_text(value)
    return node


def _append_decimal(parent: ET.Element, tag: str, value: object | None) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.text = _decimal_to_str(_money_decimal_from_cents(value))
    return node


def _invoice_partner_dict(invoice: Any) -> dict[str, str]:
    contact = getattr(invoice, "contact", None)
    return {
        "name": _value_text(getattr(contact, "name", None) or getattr(invoice, "buyer_name_cache", None)),
        "street": _value_text(getattr(contact, "street", None)),
        "city": _value_text(getattr(contact, "city", None)),
        "zip": _value_text(getattr(contact, "zip", None)),
        "country": _safe_country(getattr(contact, "country", None)),
        "ico": _value_text(getattr(contact, "ico", None) or getattr(invoice, "buyer_registration_no_cache", None)),
        "dic": _value_text(getattr(contact, "dic", None)),
        "email": _value_text(getattr(contact, "email", None)),
        "phone": _value_text(getattr(contact, "phone", None)),
    }


def _payment_method_name(value: object | None) -> str:
    normalized = _value_text(value).lower()
    return {
        "bank_transfer": "Bank transfer",
        "cash": "Cash",
        "card": "Card",
        "cod": "Cash on delivery",
    }.get(normalized, normalized or "Bank transfer")


def build_pohoda_invoice_export_bytes(
    *,
    invoices: list[Any],
    items_by_invoice: dict[int, list[dict[str, object]]],
    subject_id: int,
) -> bytes:
    ns = {
        "dat": "http://www.stormware.cz/schema/version_2/data.xsd",
        "inv": "http://www.stormware.cz/schema/version_2/invoice.xsd",
        "typ": "http://www.stormware.cz/schema/version_2/type.xsd",
    }
    for prefix, uri in ns.items():
        ET.register_namespace(prefix, uri)

    root = ET.Element(
        ET.QName(ns["dat"], "dataPack"),
        {
            "id": f"fakturek-{int(subject_id)}-{utc_now().strftime('%Y%m%d%H%M%S')}",
            "ico": "",
            "application": "Fakturek",
            "version": "2.0",
            "note": "Basic invoice export for POHODA",
        },
    )

    for invoice in invoices:
        invoice_id = int(getattr(invoice, "id", 0) or 0)
        partner = _invoice_partner_dict(invoice)
        pack_item = ET.SubElement(
            root,
            ET.QName(ns["dat"], "dataPackItem"),
            {
                "id": f"invoice-{invoice_id}",
                "version": "2.0",
            },
        )
        inv_el = ET.SubElement(pack_item, ET.QName(ns["inv"], "invoice"), {"version": "2.0"})

        header = ET.SubElement(inv_el, ET.QName(ns["inv"], "invoiceHeader"))
        _append_text(header, ET.QName(ns["inv"], "invoiceType"), "issuedInvoice")
        number = ET.SubElement(header, ET.QName(ns["inv"], "number"))
        _append_text(number, ET.QName(ns["typ"], "numberRequested"), getattr(invoice, "number", None))
        _append_text(header, ET.QName(ns["inv"], "symVar"), getattr(invoice, "variable_symbol", None) or getattr(invoice, "number", None))
        _append_text(header, ET.QName(ns["inv"], "date"), _date_text(getattr(invoice, "issue_date", None)))
        _append_text(header, ET.QName(ns["inv"], "dateTax"), _date_text(getattr(invoice, "issue_date", None)))
        _append_text(header, ET.QName(ns["inv"], "dateDue"), _date_text(getattr(invoice, "due_date", None)))
        _append_text(header, ET.QName(ns["inv"], "text"), partner["name"] or getattr(invoice, "number", None))
        _append_text(header, ET.QName(ns["inv"], "paymentType"), _payment_method_name(getattr(invoice, "payment_method", None)))
        _append_text(header, ET.QName(ns["inv"], "note"), getattr(invoice, "notes", None))

        partner_identity = ET.SubElement(header, ET.QName(ns["inv"], "partnerIdentity"))
        address = ET.SubElement(partner_identity, ET.QName(ns["typ"], "address"))
        _append_text(address, ET.QName(ns["typ"], "company"), partner["name"])
        _append_text(address, ET.QName(ns["typ"], "name"), partner["name"])
        _append_text(address, ET.QName(ns["typ"], "street"), partner["street"])
        _append_text(address, ET.QName(ns["typ"], "city"), partner["city"])
        _append_text(address, ET.QName(ns["typ"], "zip"), partner["zip"])
        _append_text(address, ET.QName(ns["typ"], "ico"), partner["ico"])
        _append_text(address, ET.QName(ns["typ"], "dic"), partner["dic"])
        _append_text(address, ET.QName(ns["typ"], "email"), partner["email"])
        _append_text(address, ET.QName(ns["typ"], "mobilPhone"), partner["phone"])
        _append_text(address, ET.QName(ns["typ"], "country"), partner["country"])

        if _value_text(getattr(invoice, "bank_account_number", None)) or _value_text(getattr(invoice, "bank_account_iban", None)):
            account = ET.SubElement(header, ET.QName(ns["inv"], "account"))
            _append_text(account, ET.QName(ns["typ"], "accountNo"), getattr(invoice, "bank_account_number", None))
            _append_text(account, ET.QName(ns["typ"], "iban"), getattr(invoice, "bank_account_iban", None))
            _append_text(account, ET.QName(ns["typ"], "swift"), getattr(invoice, "bank_account_bic", None))

        detail = ET.SubElement(inv_el, ET.QName(ns["inv"], "invoiceDetail"))
        line_rows = items_by_invoice.get(invoice_id, [])
        for item_row in line_rows:
            item_el = ET.SubElement(detail, ET.QName(ns["inv"], "invoiceItem"))
            _append_text(item_el, ET.QName(ns["inv"], "text"), item_row.get("description"))
            _append_text(item_el, ET.QName(ns["inv"], "quantity"), item_row.get("quantity"))
            _append_text(item_el, ET.QName(ns["inv"], "unit"), item_row.get("unit"))
            _append_text(item_el, ET.QName(ns["inv"], "payVAT"), "true")
            home_currency = ET.SubElement(item_el, ET.QName(ns["inv"], "homeCurrency"))
            _append_text(home_currency, ET.QName(ns["typ"], "unitPrice"), item_row.get("unit_price"))
            _append_text(home_currency, ET.QName(ns["typ"], "price"), item_row.get("line_total"))
            _append_text(home_currency, ET.QName(ns["typ"], "priceSum"), item_row.get("line_total"))
            vat_rate = _value_text(item_row.get("vat_rate"))
            if vat_rate:
                _append_text(item_el, ET.QName(ns["inv"], "rateVAT"), vat_rate)

        summary = ET.SubElement(inv_el, ET.QName(ns["inv"], "invoiceSummary"))
        home_currency = ET.SubElement(summary, ET.QName(ns["inv"], "homeCurrency"))
        _append_decimal(home_currency, ET.QName(ns["typ"], "priceNone"), getattr(invoice, "total_cents", None))
        _append_decimal(home_currency, ET.QName(ns["typ"], "round"), getattr(invoice, "rounding_adjustment_cents", None))

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_money_s3_invoice_export_bytes(
    *,
    invoices: list[Any],
    items_by_invoice: dict[int, list[dict[str, object]]],
    subject_id: int,
) -> bytes:
    root = ET.Element(
        "MoneyData",
        {
            "version": "1.0",
            "generatedBy": "Fakturek",
            "subjectId": str(int(subject_id)),
            "generatedAtUtc": utc_now().isoformat(timespec="seconds"),
        },
    )
    invoices_el = ET.SubElement(root, "SeznamFaktVyd")

    for invoice in invoices:
        invoice_id = int(getattr(invoice, "id", 0) or 0)
        partner = _invoice_partner_dict(invoice)
        inv_el = ET.SubElement(invoices_el, "FaktVyd")
        _append_text(inv_el, "Doklad", getattr(invoice, "number", None))
        _append_text(inv_el, "TypDokladu", getattr(invoice, "document_type", None) or "invoice")
        _append_text(inv_el, "Stav", getattr(invoice, "status", None))
        _append_text(inv_el, "Popis", getattr(invoice, "notes", None) or partner["name"])
        _append_text(inv_el, "Vystaveno", _date_text(getattr(invoice, "issue_date", None)))
        _append_text(inv_el, "Splatnost", _date_text(getattr(invoice, "due_date", None)))
        _append_text(inv_el, "DatumUhrady", _date_text(getattr(invoice, "paid_on", None)))
        _append_text(inv_el, "Mena", getattr(invoice, "currency", None))
        _append_text(inv_el, "VarSymbol", getattr(invoice, "variable_symbol", None) or getattr(invoice, "number", None))
        _append_decimal(inv_el, "Celkem", getattr(invoice, "total_cents", None))
        _append_decimal(inv_el, "Zaokrouhleni", getattr(invoice, "rounding_adjustment_cents", None))
        _append_text(inv_el, "ZpusobUhrady", _payment_method_name(getattr(invoice, "payment_method", None)))

        partner_el = ET.SubElement(inv_el, "Partner")
        _append_text(partner_el, "Nazev", partner["name"])
        _append_text(partner_el, "Ulice", partner["street"])
        _append_text(partner_el, "Mesto", partner["city"])
        _append_text(partner_el, "PSC", partner["zip"])
        _append_text(partner_el, "Stat", partner["country"])
        _append_text(partner_el, "ICO", partner["ico"])
        _append_text(partner_el, "DIC", partner["dic"])
        _append_text(partner_el, "Email", partner["email"])
        _append_text(partner_el, "Telefon", partner["phone"])

        if _value_text(getattr(invoice, "bank_account_number", None)) or _value_text(getattr(invoice, "bank_account_iban", None)):
            payment_el = ET.SubElement(inv_el, "BankovniUcet")
            _append_text(payment_el, "Nazev", getattr(invoice, "bank_account_label", None))
            _append_text(payment_el, "CisloUctu", getattr(invoice, "bank_account_number", None))
            _append_text(payment_el, "IBAN", getattr(invoice, "bank_account_iban", None))
            _append_text(payment_el, "BIC", getattr(invoice, "bank_account_bic", None))
            _append_text(payment_el, "Stat", getattr(invoice, "bank_account_country", None))

        items_el = ET.SubElement(inv_el, "Polozky")
        for line_no, item_row in enumerate(items_by_invoice.get(invoice_id, []), start=1):
            item_el = ET.SubElement(items_el, "Polozka")
            _append_text(item_el, "Poradi", line_no)
            _append_text(item_el, "Nazev", item_row.get("description"))
            _append_text(item_el, "Mnozstvi", item_row.get("quantity"))
            _append_text(item_el, "MJ", item_row.get("unit"))
            _append_text(item_el, "CenaMJ", item_row.get("unit_price"))
            _append_text(item_el, "SazbaDPH", item_row.get("vat_rate"))
            _append_text(item_el, "CenaCelkem", item_row.get("line_total"))

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

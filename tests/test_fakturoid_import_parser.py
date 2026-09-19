from __future__ import annotations

from datetime import date
import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from fakturek.fakturoid_import import (
    _payload_to_xml_bytes,
    _safe_resolve_under_root,
    detect_xml_import_format,
    parse_fakturek_invoices_xml_legacy,
    parse_fakturek_invoices_xml_v1,
    parse_fakturoid_invoices_xml,
    parse_money_s3_invoices_xml,
    parse_pohoda_invoices_xml,
)


SAMPLE_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<invoices>
  <invoice>
    <id>27</id>
    <number>2023-0021</number>
    <status>sent</status>
    <issued_on>2023-11-30</issued_on>
    <due_on>2023-12-14</due_on>
    <sent_at>2023-12-01T09:05:47.117+01:00</sent_at>
    <currency>CZK</currency>
    <total>11000.0</total>
    <note>Fakturujeme Vám následující položky</note>
    <private_note>internal</private_note>
    <subject_id>16</subject_id>
    <client_name>Apple Czech s.r.o.</client_name>
    <client_street>Klimentská 1216/46</client_street>
    <client_city>Praha</client_city>
    <client_zip>11000</client_zip>
    <client_country>CZ</client_country>
    <client_registration_no>28897501</client_registration_no>
    <client_vat_no>CZ28897501</client_vat_no>
    <lines>
      <line>
        <id>46</id>
        <name>Grafická karta</name>
        <quantity>1.0</quantity>
        <unit_price>8264.0</unit_price>
        <vat_rate>21</vat_rate>
        <total_price_without_vat>8264.0</total_price_without_vat>
        <total_vat>1736.0</total_vat>
      </line>
      <line>
        <id>47</id>
        <name>Jídlo</name>
        <quantity>5.0</quantity>
        <unit_price>173.92</unit_price>
        <vat_rate>15</vat_rate>
        <total_price_without_vat>869.6</total_price_without_vat>
        <total_vat>130.4</total_vat>
      </line>
    </lines>
  </invoice>
</invoices>
"""
).encode("utf-8")

POHODA_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<dat:dataPack xmlns:dat="http://www.stormware.cz/schema/version_2/data.xsd"
  xmlns:inv="http://www.stormware.cz/schema/version_2/invoice.xsd"
  xmlns:typ="http://www.stormware.cz/schema/version_2/type.xsd">
  <dat:dataPackItem id="invoice-1">
    <inv:invoice>
      <inv:invoiceHeader>
        <inv:number>
          <typ:numberRequested>2026-0101</typ:numberRequested>
        </inv:number>
        <inv:symVar>20260101</inv:symVar>
        <inv:date>2026-05-10</inv:date>
        <inv:dateDue>2026-05-20</inv:dateDue>
        <inv:text>Support retainer</inv:text>
        <inv:paymentType>Bank transfer</inv:paymentType>
        <inv:partnerIdentity>
          <typ:address>
            <typ:company>Acme s.r.o.</typ:company>
            <typ:street>Narodni 1</typ:street>
            <typ:city>Praha</typ:city>
            <typ:zip>11000</typ:zip>
            <typ:ico>12345678</typ:ico>
            <typ:dic>CZ12345678</typ:dic>
            <typ:email>billing@acme.test</typ:email>
            <typ:mobilPhone>+420123456789</typ:mobilPhone>
            <typ:country>CZ</typ:country>
          </typ:address>
        </inv:partnerIdentity>
        <inv:account>
          <typ:accountNo>2200041594/2010</typ:accountNo>
          <typ:iban>CZ4202010000002200041594</typ:iban>
          <typ:swift>FIOBCZPPXXX</typ:swift>
        </inv:account>
      </inv:invoiceHeader>
      <inv:invoiceDetail>
        <inv:invoiceItem>
          <inv:text>Membership fee</inv:text>
          <inv:quantity>1</inv:quantity>
          <inv:unit>pcs</inv:unit>
          <inv:rateVAT>0</inv:rateVAT>
          <inv:homeCurrency>
            <typ:unitPrice>72.00</typ:unitPrice>
            <typ:price>72.00</typ:price>
            <typ:priceSum>72.00</typ:priceSum>
          </inv:homeCurrency>
        </inv:invoiceItem>
      </inv:invoiceDetail>
      <inv:invoiceSummary>
        <inv:homeCurrency>
          <typ:priceNone>72.00</typ:priceNone>
        </inv:homeCurrency>
      </inv:invoiceSummary>
    </inv:invoice>
  </dat:dataPackItem>
</dat:dataPack>
"""
).encode("utf-8")

MONEY_S3_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<MoneyData version="1.0">
  <SeznamFaktVyd>
    <FaktVyd>
      <Doklad>2026-0202</Doklad>
      <Stav>paid</Stav>
      <Popis>Hosting retainer</Popis>
      <Vystaveno>2026-05-11</Vystaveno>
      <Splatnost>2026-05-18</Splatnost>
      <DatumUhrady>2026-05-12</DatumUhrady>
      <Mena>EUR</Mena>
      <VarSymbol>4470</VarSymbol>
      <Celkem>120.00</Celkem>
      <ZpusobUhrady>bank_transfer</ZpusobUhrady>
      <Partner>
        <Nazev>Red Com PMMR Ltda.</Nazev>
        <Ulice>Avenida Paulista, 1079</Ulice>
        <Mesto>Sao Paulo</Mesto>
        <PSC>01311-200</PSC>
        <Stat>BR</Stat>
        <ICO>39357251000163</ICO>
        <DIC>BR39357251000163</DIC>
        <Email>compras@redcom.digital</Email>
        <Telefon>+5511933268575</Telefon>
      </Partner>
      <BankovniUcet>
        <CisloUctu>2601502873/8330</CisloUctu>
        <IBAN>SK2083300000002601502873</IBAN>
        <BIC>FIOZSKBA</BIC>
      </BankovniUcet>
      <Polozky>
        <Polozka>
          <Nazev>Membership fee</Nazev>
          <Mnozstvi>1</Mnozstvi>
          <MJ>month</MJ>
          <CenaMJ>120.00</CenaMJ>
          <SazbaDPH>0</SazbaDPH>
          <CenaCelkem>120.00</CenaCelkem>
        </Polozka>
      </Polozky>
    </FaktVyd>
  </SeznamFaktVyd>
</MoneyData>
"""
).encode("utf-8")


FAKTUREK_XML_V1 = (
    """<?xml version="1.0" encoding="UTF-8"?>
<fakturek_export kind="invoice_export" format="fakturek_invoice_export" version="1"
  origin_subject_id="12" generated_at_utc="2026-09-19T10:00:00+00:00">
  <invoices count="1">
    <invoice id="42" number="2026-0042" document_type="invoice" status="cancelled">
      <issue_date>2026-09-01</issue_date>
      <taxable_supply_date>2026-08-31</taxable_supply_date>
      <due_date>2026-09-15</due_date>
      <paid_on></paid_on>
      <sent_at>2026-09-02T08:30:00+02:00</sent_at>
      <currency>CZK</currency>
      <variable_symbol>20260042</variable_symbol>
      <payment_method>bank_transfer</payment_method>
      <total_cents>12000</total_cents>
      <discount_cents>100</discount_cents>
      <rounding_adjustment_cents>0</rounding_adjustment_cents>
      <notes>Poznámka</notes>
      <internal_notes>Interní</internal_notes>
      <source_invoice_number>2026-0001</source_invoice_number>
      <source_invoice_id>1</source_invoice_id>
      <contact>
        <id>8</id><name>Acme s.r.o.</name><email>billing@acme.test</email>
        <phone>+420123456789</phone><street>Národní 1</street><city>Praha</city>
        <zip>11000</zip><country>CZ</country><ico>12345678</ico><dic>CZ12345678</dic>
      </contact>
      <bank_account>
        <label>Hlavní účet</label><number>123456789/0100</number>
        <iban>CZ6508000000192000145399</iban><bic>GIBACZPX</bic><country>CZ</country>
      </bank_account>
      <items count="1">
        <item line_no="1">
          <description>Členský příspěvek</description><quantity>1</quantity><unit>ks</unit>
          <unit_price_cents>10000</unit_price_cents><vat_rate>21</vat_rate>
          <line_net_cents>10000</line_net_cents><line_vat_cents>2100</line_vat_cents>
          <line_total_cents>12100</line_total_cents>
        </item>
      </items>
    </invoice>
  </invoices>
</fakturek_export>
"""
).encode("utf-8")


FAKTUREK_XML_LEGACY = (
    """<?xml version="1.0" encoding="UTF-8"?>
<fakturek_export kind="invoice_export" subject_id="12" generated_at_utc="2026-09-18T10:00:00+00:00">
  <invoices count="1">
    <invoice id="42" number="2026-0042" document_type="invoice" status="paid">
      <issue_date>2026-09-01</issue_date><due_date>2026-09-15</due_date><paid_on>2026-09-03</paid_on>
      <currency>CZK</currency><total_cents>12100</total_cents><discount_cents>0</discount_cents>
      <rounding_adjustment_cents>0</rounding_adjustment_cents><notes>Poznámka</notes><internal_notes></internal_notes>
      <contact><id>8</id><name>Acme s.r.o.</name><email>billing@acme.test</email><ico>12345678</ico></contact>
      <items count="1"><item line_no="1"><description>Položka</description><quantity>1</quantity><unit>ks</unit>
        <unit_price_cents>10000</unit_price_cents><vat_rate>21</vat_rate><line_net_cents>10000</line_net_cents>
        <line_vat_cents>2100</line_vat_cents><line_total_cents>12100</line_total_cents></item></items>
    </invoice>
  </invoices>
</fakturek_export>
"""
).encode("utf-8")


def test_parse_fakturoid_xml_basic():
    invs = parse_fakturoid_invoices_xml(SAMPLE_XML)
    assert len(invs) == 1
    inv = invs[0]
    assert inv.external_id == "27"
    assert inv.number == "2023-0021"
    assert inv.currency == "CZK"
    assert inv.status == "sent"
    assert inv.buyer_external_id == "16"
    assert inv.buyer.name.startswith("Apple")
    assert len(inv.lines) == 2


def test_zip_extract_then_parse():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("invoices.xml", SAMPLE_XML)
    zip_bytes = buf.getvalue()

    name, xml = _payload_to_xml_bytes("invoices.zip", zip_bytes)
    assert name.endswith(".xml")

    invs = parse_fakturoid_invoices_xml(xml)
    assert len(invs) == 1


def test_detect_xml_import_format_variants():
    assert detect_xml_import_format(SAMPLE_XML) == "fakturoid"
    assert detect_xml_import_format(POHODA_XML) == "pohoda_xml"
    assert detect_xml_import_format(MONEY_S3_XML) == "money_s3_xml"
    assert detect_xml_import_format(FAKTUREK_XML_V1) == "fakturek_xml_v1"
    assert detect_xml_import_format(FAKTUREK_XML_LEGACY) == "fakturek_xml_legacy"


def test_detect_fakturek_xml_rejects_unknown_version_and_kind():
    with pytest.raises(ValueError, match="verze"):
        detect_xml_import_format(FAKTUREK_XML_V1.replace(b'version="1"', b'version="2"'))
    with pytest.raises(ValueError, match="typ"):
        detect_xml_import_format(FAKTUREK_XML_V1.replace(b'kind="invoice_export"', b'kind="contacts"'))


def test_parse_fakturek_xml_v1_preserves_native_semantics():
    invoices = parse_fakturek_invoices_xml_v1(FAKTUREK_XML_V1)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.external_id == "v1:12:invoice:42"
    assert invoice.buyer_external_id == "v1:12:contact:8"
    assert invoice.number == "2026-0042"
    assert invoice.document_type == "invoice"
    assert invoice.status == "cancelled"
    assert invoice.taxable_supply_date == date(2026, 8, 31)
    assert invoice.variable_symbol == "20260042"
    assert invoice.discount_cents == 100
    assert invoice.rounding_adjustment_cents == 0
    assert invoice.buyer.phone == "+420123456789"
    assert invoice.buyer.dic == "CZ12345678"
    assert invoice.bank_account_label == "Hlavní účet"
    assert invoice.bank_account == "123456789/0100"
    assert invoice.iban == "CZ6508000000192000145399"
    assert invoice.source_invoice_external_id == "v1:12:invoice:1"
    assert invoice.lines[0].total_cents == 12100


def test_parse_legacy_fakturek_export_reads_attributes_and_nested_contact():
    invoices = parse_fakturek_invoices_xml_legacy(FAKTUREK_XML_LEGACY)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.external_id == "legacy:12:invoice:42"
    assert invoice.number == "2026-0042"
    assert invoice.status == "paid"
    assert invoice.buyer.name == "Acme s.r.o."
    assert invoice.lines[0].description == "Položka"


def test_parse_fakturek_xml_v1_rejects_tampered_totals_and_duplicate_ids():
    with pytest.raises(ValueError, match="neodpovídá total_cents"):
        parse_fakturek_invoices_xml_v1(FAKTUREK_XML_V1.replace(b"<total_cents>12000", b"<total_cents>11999"))
    duplicated = FAKTUREK_XML_V1.replace(
        b"</invoices>",
        FAKTUREK_XML_V1.split(b"<invoice ", 1)[1].split(b"</invoice>", 1)[0].join([b"<invoice ", b"</invoice>"]) + b"</invoices>",
    )
    duplicated = duplicated.replace(b'<invoices count="1">', b'<invoices count="2">')
    with pytest.raises(ValueError, match="duplicitní invoice@id"):
        parse_fakturek_invoices_xml_v1(duplicated)

    noncanonical_line = FAKTUREK_XML_V1.replace(
        b"<unit_price_cents>10000",
        b"<unit_price_cents>9999",
    )
    with pytest.raises(ValueError, match="množství, ceně a DPH"):
        parse_fakturek_invoices_xml_v1(noncanonical_line)

    excessive_discount = (
        FAKTUREK_XML_V1.replace(b"<discount_cents>100", b"<discount_cents>20000")
        .replace(b"<rounding_adjustment_cents>0", b"<rounding_adjustment_cents>19900")
    )
    with pytest.raises(ValueError, match="sleva"):
        parse_fakturek_invoices_xml_v1(excessive_discount)


def test_parse_fakturek_xml_v1_supports_signed_credit_note_and_rejects_unknown_enums():
    root = ET.fromstring(FAKTUREK_XML_V1)
    invoices_el = root.find("./invoices")
    assert invoices_el is not None
    credit_el = invoices_el.find("./invoice")
    assert credit_el is not None

    source_el = ET.fromstring(ET.tostring(credit_el, encoding="utf-8"))
    source_el.attrib.update(id="1", number="2026-0001", document_type="invoice", status="issued")
    source_el.find("./source_invoice_number").text = ""
    source_el.find("./source_invoice_id").text = ""
    invoices_el.insert(0, source_el)
    invoices_el.attrib["count"] = "2"

    credit_el.attrib["document_type"] = "credit_note"
    credit_el.find("./total_cents").text = "-12100"
    credit_el.find("./discount_cents").text = "0"
    credit_el.find("./items/item/unit_price_cents").text = "-10000"
    credit_el.find("./items/item/line_net_cents").text = "-10000"
    credit_el.find("./items/item/line_vat_cents").text = "-2100"
    credit_el.find("./items/item/line_total_cents").text = "-12100"

    credit_note_pair = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    parsed = parse_fakturek_invoices_xml_v1(credit_note_pair)
    parsed_credit = next(invoice for invoice in parsed if invoice.document_type == "credit_note")
    assert parsed_credit.total_cents == -12100
    assert parsed_credit.lines[0].unit_price_cents == -10000

    mismatched_source_number = credit_note_pair.replace(
        b"<source_invoice_number>2026-0001</source_invoice_number>",
        b"<source_invoice_number>2026-TYPO</source_invoice_number>",
    )
    with pytest.raises(ValueError, match="neodpovídá source_invoice_number"):
        parse_fakturek_invoices_xml_v1(mismatched_source_number)

    with pytest.raises(ValueError, match="document_type"):
        parse_fakturek_invoices_xml_v1(
            FAKTUREK_XML_V1.replace(b'document_type="invoice"', b'document_type="unsupported"')
        )
    with pytest.raises(ValueError, match="status"):
        parse_fakturek_invoices_xml_v1(
            FAKTUREK_XML_V1.replace(b'status="cancelled"', b'status="unknown"')
        )


def test_parse_pohoda_xml_basic():
    invs = parse_pohoda_invoices_xml(POHODA_XML)
    assert len(invs) == 1
    inv = invs[0]
    assert inv.external_id == "pohoda:2026-0101"
    assert inv.number == "2026-0101"
    assert inv.variable_symbol == "20260101"
    assert inv.buyer.name == "Acme s.r.o."
    assert inv.buyer.ico == "12345678"
    assert inv.bank_account == "2200041594/2010"
    assert inv.iban == "CZ4202010000002200041594"
    assert len(inv.lines) == 1
    assert inv.lines[0].unit == "pcs"
    assert inv.total_cents == 7200


def test_parse_money_s3_xml_basic():
    invs = parse_money_s3_invoices_xml(MONEY_S3_XML)
    assert len(invs) == 1
    inv = invs[0]
    assert inv.external_id == "money_s3:2026-0202"
    assert inv.number == "2026-0202"
    assert inv.variable_symbol == "4470"
    assert inv.currency == "EUR"
    assert inv.status == "paid"
    assert inv.paid_on is not None
    assert inv.buyer.name == "Red Com PMMR Ltda."
    assert inv.iban == "SK2083300000002601502873"
    assert len(inv.lines) == 1
    assert inv.lines[0].unit == "month"
    assert inv.total_cents == 12000


def test_xml_safety_rejects_doctype_after_long_prefix():
    from fakturek.security import ensure_safe_xml_bytes

    payload = b"<!--" + (b"a" * 9000) + b"--><!DOCTYPE root><root/>"
    with pytest.raises(ValueError):
        ensure_safe_xml_bytes(payload)


def test_import_storage_path_cannot_escape_to_sibling_directory(tmp_path):
    storage_root = tmp_path / "imports"
    storage_root.mkdir()
    sibling = tmp_path / "imports-private" / "secret.xml"
    sibling.parent.mkdir()
    sibling.write_text("secret")

    with pytest.raises(ValueError, match="mimo import storage"):
        _safe_resolve_under_root(storage_root, "../imports-private/secret.xml")

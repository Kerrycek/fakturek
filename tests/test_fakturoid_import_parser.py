from __future__ import annotations

import io
import zipfile

import pytest

from fakturek.fakturoid_import import (
    _payload_to_xml_bytes,
    detect_xml_import_format,
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

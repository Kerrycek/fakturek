from __future__ import annotations

import io
import zipfile

from fakturek.fakturoid_import import (
    _payload_to_assets,
    infer_series_from_invoice_numbers,
    inspect_contacts_csv,
    parse_contacts_csv_with_mapping,
    parse_fakturoid_contacts_csv,
    parse_fakturoid_contacts_xml,
)


SAMPLE_CSV_COMMA = (
    "id,name,email,street,city,zip,country,registration_no,vat_no,phone\n"
    "16,Apple Czech s.r.o.,info@example.com,Klimentská 1216/46,Praha,11000,CZ,28897501,CZ28897501,+420123\n"
).encode("utf-8")


SAMPLE_CSV_SEMI = (
    "id;company_name;mail;address;town;postal_code;country;ico;dic;telephone\n"
    "99;Foo s.r.o.;foo@example.com;Ulice 1;Brno;60200;CZ;12345678;CZ12345678;+420999\n"
).encode("utf-8")


SAMPLE_CONTACTS_XML = (
    """<?xml version="1.0" encoding="UTF-8"?>
<contacts>
  <contact>
    <id>16</id>
    <company_name>Apple Czech s.r.o.</company_name>
    <email>info@example.com</email>
    <street>Klimentská 1216/46</street>
    <city>Praha</city>
    <postal_code>11000</postal_code>
    <country>CZ</country>
    <registration_no>28897501</registration_no>
    <vat_no>CZ28897501</vat_no>
    <telephone>+420123</telephone>
  </contact>
</contacts>
"""
).encode("utf-8")


def test_parse_contacts_csv_comma():
    contacts = parse_fakturoid_contacts_csv(SAMPLE_CSV_COMMA)
    assert len(contacts) == 1
    c = contacts[0]
    assert c.external_id == "16"
    assert c.name.startswith("Apple")
    assert c.email == "info@example.com"
    assert c.ico == "28897501"
    assert c.dic == "CZ28897501"


def test_parse_contacts_csv_semicolon():
    contacts = parse_fakturoid_contacts_csv(SAMPLE_CSV_SEMI)
    assert len(contacts) == 1
    c = contacts[0]
    assert c.external_id == "99"
    assert c.name == "Foo s.r.o."
    assert c.city == "Brno"


def test_parse_contacts_csv_with_custom_mapping():
    sample = (
        "Company,E-mail,Street,Postal,CityName,CountryCode,CompanyID,TaxID,PhoneNumber,Ref\n"
        "Red Com,compras@example.test,Avenida Paulista,01311-200,Sao Paulo,BR,39357251000163,BR39357251000163,+5511999,4470\n"
    ).encode("utf-8")
    contacts = parse_contacts_csv_with_mapping(
        sample,
        mapping={
            "name": "Company",
            "email": "E-mail",
            "street": "Street",
            "zip": "Postal",
            "city": "CityName",
            "country": "CountryCode",
            "ico": "CompanyID",
            "dic": "TaxID",
            "phone": "PhoneNumber",
            "fixed_variable_symbol": "Ref",
        },
    )
    assert len(contacts) == 1
    c = contacts[0]
    assert c.name == "Red Com"
    assert c.email == "compras@example.test"
    assert c.country == "BR"
    assert c.fixed_variable_symbol == "4470"


def test_inspect_contacts_csv_returns_headers_and_inferred_mapping():
    meta = inspect_contacts_csv(SAMPLE_CSV_SEMI)
    assert "company_name" in meta["headers"]
    assert meta["row_count"] == 1
    inferred = meta["inferred_mapping"]
    assert inferred["name"] == "company_name"
    assert inferred["email"] == "mail"


def test_parse_contacts_xml():
    contacts = parse_fakturoid_contacts_xml(SAMPLE_CONTACTS_XML)
    assert len(contacts) == 1
    c = contacts[0]
    assert c.external_id == "16"
    assert c.name == "Apple Czech s.r.o."
    assert c.email == "info@example.com"
    assert c.ico == "28897501"
    assert c.dic == "CZ28897501"


def test_payload_to_assets_zip_with_xml_and_csv():
    # We don't care about the XML content here; it just needs to look like XML.
    sample_xml = b"<?xml version='1.0'?><invoices></invoices>"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("invoices.xml", sample_xml)
        zf.writestr("contacts.csv", SAMPLE_CSV_COMMA)

    assets = _payload_to_assets("export.zip", buf.getvalue())
    assert assets.xml_bytes is not None
    assert assets.csv_bytes is not None
    assert str(assets.xml_name).endswith(".xml")
    assert str(assets.csv_name).endswith(".csv")


def test_infer_series_from_numbers_basic():
    inferred = infer_series_from_invoice_numbers(
        ["2023-0021", "2023-0001", "0007", "S-0099", "S-10000"]
    )
    assert inferred["2023-"].pad_length == 4
    assert inferred["2023-"].max_counter == 21
    assert inferred[""].pad_length == 4
    assert inferred[""].max_counter == 7
    assert inferred["S-"].pad_length == 4
    assert inferred["S-"].max_counter == 10000

from __future__ import annotations

import csv
import io
import sys
from types import SimpleNamespace

import pytest


def _contact(**overrides):
    values = {
        "id": 42,
        "external_id": "CRM-9",
        "external_source": "crm",
        "name": "Acme",
        "email": "info@example.test",
        "phone": "+420123456789",
        "street": "Main 1",
        "city": "Prague",
        "zip": "11000",
        "country": "CZ",
        "ico": "12345678",
        "dic": "CZ12345678",
        "fixed_variable_symbol": "001234",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_contacts_csv_round_trip_is_exact_safe_and_has_no_internal_identity():
    from fakturek.contacts_csv import (
        BUSINESS_FIELDS,
        HEADER,
        build_contacts_csv_bytes,
        parse_contacts_csv_bytes,
    )

    contact = _contact(
        name="=ACME",
        email="+mail@example.test",
        phone="-123",
        street="@Main",
        city="\tPrague",
        zip="\r11000",
        ico="\n12345678",
        dic="'=literal",
    )
    payload = build_contacts_csv_bytes(contacts=[contact])
    decoded = payload.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=";", strict=True)
    assert next(reader) == list(HEADER)
    exported = next(reader)
    assert exported[0] == "1"
    assert exported[1].startswith("'=")
    assert exported[2].startswith("'+")
    assert exported[3].startswith("'-")
    assert exported[4].startswith("'@")
    assert exported[5].startswith("'\t")
    assert exported[6].startswith("'\r")
    assert exported[8].startswith("'\n")
    assert exported[9] == "''=literal"
    assert "id" not in HEADER
    assert "external_id" not in HEADER
    assert "external_source" not in HEADER

    [parsed] = parse_contacts_csv_bytes(payload)
    assert parsed.as_dict() == {field: getattr(contact, field) for field in BUSINESS_FIELDS}


def test_contacts_csv_preserves_genuine_leading_apostrophes_and_is_deterministic():
    from fakturek.contacts_csv import build_contacts_csv_bytes, parse_contacts_csv_bytes

    first = _contact(name="'=literal", email="''two@example.test", phone="'")
    second = _contact(name="Beta", email="beta@example.test", fixed_variable_symbol="987")
    forward = build_contacts_csv_bytes(contacts=[first, second])
    reverse = build_contacts_csv_bytes(contacts=[second, first])
    assert forward == reverse
    parsed = parse_contacts_csv_bytes(forward)
    assert {row.name: row.as_dict() for row in parsed} == {
        first.name: {
            field: str(getattr(first, field, "") or "")
            for field in parsed[0].as_dict()
        },
        second.name: {
            field: str(getattr(second, field, "") or "")
            for field in parsed[0].as_dict()
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"name;email\nAcme;a@example.test\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol;extra\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n2;Acme;;;;;;;;;\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n1;\xff;;;;;;;;;\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n1;;;;;;;;;;\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n1;Acme;;;;;;;;\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n1;\"unterminated;;;;;;;;;\n",
        b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n1;Acme;bad\x00value;;;;;;;;\n",
        (
            b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n"
            b"fakturek_contacts_csv_version;name;email;phone;street;city;zip;country;ico;dic;fixed_variable_symbol\n"
        ),
    ],
)
def test_contacts_csv_rejects_malformed_or_non_v1_payloads(payload):
    from fakturek.contacts_csv import parse_contacts_csv_bytes

    with pytest.raises(ValueError):
        parse_contacts_csv_bytes(payload)


def test_contacts_csv_allows_only_trailing_blank_rows():
    from fakturek.contacts_csv import HEADER, parse_contacts_csv_bytes

    header = ";".join(HEADER)
    row = "1;Acme;;;;;;;;;"
    assert len(parse_contacts_csv_bytes(f"{header}\n{row}\n\n".encode())) == 1
    with pytest.raises(ValueError, match="after trailing blank"):
        parse_contacts_csv_bytes(f"{header}\n\n{row}\n".encode())


def test_contacts_csv_enforces_row_cell_field_and_file_limits(monkeypatch):
    from fakturek.contacts_csv import (
        HEADER,
        MAX_CELL_CHARS,
        build_contacts_csv_bytes,
        parse_contacts_csv_bytes,
    )
    contacts_csv = sys.modules["fakturek.contacts_csv"]

    header = ";".join(HEADER)
    row = "1;Acme;;;;;;;;;"
    two_rows = f"{header}\n{row}\n{row}\n".encode()
    with pytest.raises(ValueError, match="at most 1 rows"):
        parse_contacts_csv_bytes(two_rows, max_rows=1)
    with pytest.raises(ValueError, match="at most 1 rows"):
        build_contacts_csv_bytes(contacts=[_contact(), _contact(name="Two")], max_rows=1)

    oversized_cell = "A" * (MAX_CELL_CHARS + 1)
    with pytest.raises(ValueError, match="oversized cell"):
        parse_contacts_csv_bytes(f"{header}\n1;Acme;{oversized_cell};;;;;;;;\n".encode())
    with pytest.raises(ValueError, match="name is too long"):
        build_contacts_csv_bytes(contacts=[_contact(name="N" * 256)])
    with pytest.raises(ValueError, match="too large"):
        parse_contacts_csv_bytes(two_rows, max_upload_bytes=len(two_rows) - 1)
    with pytest.raises(ValueError, match="too large"):
        build_contacts_csv_bytes(contacts=[_contact()], max_upload_bytes=10)
    monkeypatch.setattr(contacts_csv, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ValueError, match="too large"):
        parse_contacts_csv_bytes(two_rows, max_upload_bytes=10_000)
    with pytest.raises(ValueError, match="too large"):
        build_contacts_csv_bytes(contacts=[_contact()], max_upload_bytes=10_000)


def test_contacts_csv_header_only_represents_an_empty_contact_set():
    from fakturek.contacts_csv import HEADER, build_contacts_csv_bytes, parse_contacts_csv_bytes

    payload = build_contacts_csv_bytes(contacts=[])
    assert payload.decode("utf-8-sig") == ";".join(HEADER) + "\n"
    assert parse_contacts_csv_bytes(payload) == []

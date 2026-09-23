from __future__ import annotations

from decimal import Decimal

import pytest


def _row(**changes: str) -> dict[str, str]:
    row = {
        "fakturek_catalog_csv_version": "1",
        "description": "Monthly service",
        "quantity": "1.00",
        "unit": "hour",
        "unit_price": "1500.00",
        "vat_rate": "21.00",
        "currency": "CZK",
    }
    row.update(changes)
    return row


def test_catalog_csv_export_has_exact_bom_header_and_reversible_formula_cells():
    from fakturek.catalog_csv import HEADER, build_catalog_csv_bytes, parse_catalog_csv_bytes

    item = type(
        "CatalogItem",
        (),
        {
            "description": "'=genuine apostrophe",
            "quantity": Decimal("1.00"),
            "unit": "=dangerous unit",
            "unit_price_cents": 100,
            "vat_rate": Decimal("0"),
            "currency": "CZK",
        },
    )()
    payload = build_catalog_csv_bytes(catalog_items=[item])
    assert payload.startswith(b"\xef\xbb\xbf" + ";".join(HEADER).encode() + b"\n")
    assert b"''=genuine apostrophe" in payload
    assert b"'=dangerous unit" in payload
    assert parse_catalog_csv_bytes(payload) == [
        _row(
            description="'=genuine apostrophe",
            unit="=dangerous unit",
            unit_price="1.00",
            vat_rate="0.00",
        )
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"description;quantity;unit;unit_price;vat_rate;currency\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency;extra\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n2;A;1;;1;0;CZK\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n1;A;0;;1;0;CZK\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n1;A;1.001;;1;0;CZK\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n1;A;1;;1;0;czk\n",
        b"fakturek_catalog_csv_version;description;quantity;unit;unit_price;vat_rate;currency\n1;\xff;1;;1;0;CZK\n",
    ],
)
def test_catalog_csv_parser_rejects_strict_negative_matrix(payload):
    from fakturek.catalog_csv import parse_catalog_csv_bytes

    with pytest.raises(ValueError):
        parse_catalog_csv_bytes(payload)


def test_catalog_csv_parser_allows_comma_decimals_and_only_trailing_blank_rows():
    from fakturek.catalog_csv import HEADER, parse_catalog_csv_bytes

    payload = (
        b"\xef\xbb\xbf"
        + ";".join(HEADER).encode()
        + b"\n1;Service;1,5;hour;2,50;21,0;EUR\n\n;;;;;;\n"
    )
    assert parse_catalog_csv_bytes(payload) == [
        _row(
            description="Service",
            quantity="1.50",
            unit_price="2.50",
            vat_rate="21.00",
            currency="EUR",
        )
    ]


@pytest.mark.parametrize("value", ["1e2", "1_000", "+1", "1.", ".5"])
def test_catalog_csv_rejects_non_v1_decimal_grammar(value):
    from fakturek.catalog_csv import normalise_row

    with pytest.raises(ValueError):
        normalise_row(_row(quantity=value))


def test_catalog_csv_semantic_identity_is_case_and_lexical_invariant():
    from fakturek.catalog_csv import normalise_row, row_identity

    first = normalise_row(_row(description="Monthly Service", quantity="1", unit_price="2,5"))
    second = normalise_row(_row(description="monthly service", quantity="1.00", unit_price="2.50"))
    assert row_identity(first) == row_identity(second)
    assert normalise_row(_row(unit_price="-0", vat_rate="-0"))["unit_price"] == "0.00"

from __future__ import annotations

from decimal import Decimal

import pytest

from fakturek.money import (
    compute_line_amounts_cents,
    compute_line_total_cents,
    format_cents,
    format_quantity,
    parse_money_to_cents,
    parse_money_to_signed_cents,
    parse_quantity,
    parse_vat_rate,
)


def test_parse_money_to_cents_dot_and_comma():
    assert parse_money_to_cents("123.45") == 12345
    assert parse_money_to_cents("123,45") == 12345


def test_parse_money_to_cents_blank_defaults_to_zero():
    assert parse_money_to_cents("") == 0
    assert parse_money_to_cents(None) == 0


def test_parse_money_to_signed_cents_allows_negative():
    assert parse_money_to_signed_cents("-1.23") == -123
    assert parse_money_to_signed_cents("+2,50") == 250
    assert parse_money_to_signed_cents("") == 0


def test_parse_quantity_default_and_validation():
    assert parse_quantity(None) == Decimal("1.00")
    assert parse_quantity("2") == Decimal("2.00")

    with pytest.raises(ValueError):
        parse_quantity("0")


def test_format_quantity_hides_meaningless_decimal_places():
    assert format_quantity(Decimal("1.00")) == "1"
    assert format_quantity("2.50") == "2.5"
    assert format_quantity("3,25") == "3.25"
    assert format_quantity("text") == "text"


def test_parse_vat_rate_default_and_validation():
    assert parse_vat_rate(None) == Decimal("21.00")
    assert parse_vat_rate("0") == Decimal("0.00")

    with pytest.raises(ValueError):
        parse_vat_rate("-1")

    with pytest.raises(ValueError):
        parse_vat_rate("101")


def test_compute_line_total_cents_includes_vat():
    assert (
        compute_line_total_cents(
            quantity=Decimal("2.00"),
            unit_price_cents=1000,
            vat_rate=Decimal("21.00"),
        )
        == 2420
    )


def test_compute_line_amounts_cents_splits_net_and_vat_consistently():
    net, vat, total = compute_line_amounts_cents(
        quantity=Decimal("2.00"),
        unit_price_cents=1000,
        vat_rate=Decimal("21.00"),
    )
    assert net == 2000
    assert vat == 420
    assert total == 2420
    assert net + vat == total


def test_format_cents_basic_and_currency():
    assert format_cents(0, "CZK") == "0,00 CZK"
    assert format_cents(123, "czk") == "1,23 CZK"
    assert format_cents(None, "CZK") == "0,00 CZK"


def test_format_cents_thousands_and_negative():
    assert format_cents(123456789, "EUR") == "1 234 567,89 EUR"
    assert format_cents(-123456, "CZK") == "-1 234,56 CZK"
    assert format_cents(100, None) == "1,00"

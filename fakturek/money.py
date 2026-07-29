from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _to_decimal(value: str) -> Decimal:
    """Parse user input into Decimal.

    Accepts both dot and comma as decimal separator.
    """

    v = value.strip().replace(" ", "").replace(",", ".")
    if not v:
        raise ValueError("Prázdná hodnota.")
    try:
        return Decimal(v)
    except InvalidOperation as exc:  # pragma: no cover
        raise ValueError("Neplatné číslo.") from exc


def parse_quantity(value: str | None, *, default: Decimal = Decimal("1.00")) -> Decimal:
    """Parse quantity with 2 decimal places."""

    if value is None or not value.strip():
        q = default
    else:
        q = _to_decimal(value)

    q = q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q <= 0:
        raise ValueError("Množství musí být větší než 0.")
    return q


def format_quantity(value: object | None) -> str:
    """Format invoice quantity without meaningless trailing decimals.

    Stored quantities are rounded to two decimal places, but invoices should not
    show values like ``1.00`` when the quantity is a whole number.
    """

    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        number = Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return raw
    out = format(number.normalize(), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out or "0"


def parse_vat_rate(value: str | None, *, default: Decimal = Decimal("21.00")) -> Decimal:
    """Parse VAT rate as percent with 2 decimal places."""

    if value is None or not value.strip():
        r = default
    else:
        r = _to_decimal(value)

    r = r.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if r < 0 or r > 100:
        raise ValueError("DPH musí být v rozsahu 0–100 %.")
    return r


def parse_money_to_cents(value: str | None, *, default: Decimal = Decimal("0.00")) -> int:
    """Parse a money amount (e.g. "123.45") into integer cents.

    The input is expected in major currency units (CZK/EUR/...).
    """

    if value is None or not value.strip():
        amount = default
    else:
        amount = _to_decimal(value)

    if amount < 0:
        raise ValueError("Cena nesmí být záporná.")

    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def parse_money_to_signed_cents(
    value: str | None,
    *,
    default: Decimal = Decimal("0.00"),
) -> int:
    """Parse a (possibly signed) money amount into integer cents.

    This is useful for values such as invoice rounding adjustments which may be
    negative.
    """

    if value is None or not str(value).strip():
        amount = default
    else:
        amount = _to_decimal(str(value))

    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def compute_line_total_cents(
    *,
    quantity: Decimal,
    unit_price_cents: int,
    vat_rate: Decimal,
) -> int:
    """Compute line total in cents (incl. VAT)."""

    if unit_price_cents < 0:
        raise ValueError("Cena nesmí být záporná.")
    if quantity <= 0:
        raise ValueError("Množství musí být větší než 0.")
    if vat_rate < 0:
        raise ValueError("DPH nesmí být záporné.")

    base_cents = quantity * Decimal(unit_price_cents)  # may be fractional cents
    multiplier = Decimal("1") + (vat_rate / Decimal("100"))
    total_cents = (base_cents * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(total_cents)


def compute_line_amounts_cents(
    *,
    quantity: Decimal,
    unit_price_cents: int,
    vat_rate: Decimal,
) -> tuple[int, int, int]:
    """Compute (net, vat, total) in cents.

    The early MVP stored only the gross line total. The master plan schema
    introduces explicit net/vat columns. To keep backwards-compatible totals
    (and rounding), we compute:

    - net_cents: rounded(quantity * unit_price)
    - total_cents: same as :func:`compute_line_total_cents`
    - vat_cents: total - net

    This guarantees ``net + vat == total`` while preserving the historical
    gross rounding behaviour.
    """

    # Reuse the validation + rounding logic from compute_line_total_cents.
    total_cents = compute_line_total_cents(
        quantity=quantity,
        unit_price_cents=unit_price_cents,
        vat_rate=vat_rate,
    )

    base_cents = quantity * Decimal(unit_price_cents)  # may be fractional cents
    net_cents = int(base_cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    vat_cents = int(total_cents - net_cents)
    return net_cents, vat_cents, int(total_cents)


def format_cents(amount_cents: int | None, currency: str | None = None) -> str:
    """Format integer cents to a human-friendly string.

    Uses Czech-style formatting:
    - thousands separator: space
    - decimal separator: comma

    Examples:
    - 0 -> "0,00 CZK"
    - 123456 -> "1 234,56 CZK"

    The function is intentionally dependency-free (no locale).
    """

    if amount_cents is None:
        cents_int = 0
    else:
        try:
            cents_int = int(amount_cents)
        except Exception as exc:  # pragma: no cover
            raise ValueError("Neplatná částka.") from exc

    sign = "-" if cents_int < 0 else ""
    cents_int = abs(cents_int)

    major = cents_int // 100
    minor = cents_int % 100

    major_str = f"{major:,}".replace(",", " ")
    out = f"{sign}{major_str},{minor:02d}"
    if currency:
        out = f"{out} {currency.upper()}"
    return out

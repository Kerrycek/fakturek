from __future__ import annotations

SIGNED_INTEGER_MAX = 2_147_483_647
INVOICE_SERIES_EXHAUSTED_LABEL = "Vyčerpáno — další číslo nelze přidělit"


class InvoiceSeriesCounterExhausted(ValueError):
    pass


def next_invoice_series_counter(base_counter: int, *, offset: int = 1) -> int:
    """Return a counter that remains representable by the database Integer column."""
    candidate = int(base_counter) + int(offset)
    if candidate < 1 or candidate > SIGNED_INTEGER_MAX:
        raise InvoiceSeriesCounterExhausted("invoice series counter is exhausted")
    return candidate

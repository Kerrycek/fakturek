from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Database backends such as SQLite/MySQL may persist DATETIME values without
    a timezone marker, so use ``as_utc_aware`` before comparing loaded values.
    """
    return datetime.now(UTC)


def as_utc_aware(value: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC.

    Existing installations may already contain naive UTC timestamps.  Treat
    such values as UTC to keep comparisons safe while moving the application
    code to timezone-aware timestamps.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

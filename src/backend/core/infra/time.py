"""Application instants use aware UTC; calendar schedules keep their explicit zone."""
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Normalize an instant; legacy timezone-less application values mean UTC.

    User-entered local calendar values must first be localized with their
    declared business timezone, not passed here as naive timestamps.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

"""Application instants use aware UTC; calendar schedules keep their explicit zone."""
from datetime import datetime, timezone
from typing import Optional, overload


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@overload
def as_utc(value: datetime) -> datetime: ...


@overload
def as_utc(value: None) -> None: ...


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize an instant; legacy timezone-less application values mean UTC.

    ``None`` passes through so nullable columns can be normalized in place.
    User-entered local calendar values must first be localized with their
    declared business timezone, not passed here as naive timestamps.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

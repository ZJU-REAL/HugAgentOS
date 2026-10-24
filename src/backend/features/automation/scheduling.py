"""Timezone-aware cron scheduling shared by automation and personal-agent schedules."""
from datetime import datetime
from typing import Optional
from croniter import croniter
import pytz


def compute_next_run(
    cron_expression: str,
    timezone: str = "Asia/Shanghai",
    base_time: Optional[datetime] = None,
) -> datetime:
    """Compute the next fire time from a cron expression in the given timezone.

    Returns a timezone-aware UTC datetime.
    """
    tz = pytz.timezone(timezone)
    base = base_time or datetime.now(tz)
    if base.tzinfo is None:
        base = tz.localize(base)
    cron = croniter(cron_expression, base)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(pytz.utc)

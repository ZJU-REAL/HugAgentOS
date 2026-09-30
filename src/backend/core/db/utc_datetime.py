"""UTC timestamp boundary shared by PostgreSQL and SQLite."""
from sqlalchemy import TIMESTAMP
from sqlalchemy.types import TypeDecorator

from core.infra.time import as_utc


class UTCDateTime(TypeDecorator):
    """Persist UTC instants and restore timezone metadata lost by SQLite.

    PostgreSQL retains TIMESTAMP WITH TIME ZONE, so no schema conversion or
    historical timestamp shift is performed. Legacy naive application inputs
    are interpreted as UTC at the boundary, never in the database session zone.
    """

    impl = TIMESTAMP
    cache_ok = True

    def __init__(self, timezone=True):
        if not timezone:
            raise ValueError("Application timestamps must carry a timezone")
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        return as_utc(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return as_utc(value) if value is not None else None



def render_utc_type(type_, obj, autogen_context):
    """Keep generated migrations independent of application Python types."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.TIMESTAMP(timezone=True)"
    return False

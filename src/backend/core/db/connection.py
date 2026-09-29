"""Connection settings shared by runtime, migrations and data utilities."""
from sqlalchemy.engine import make_url


def utc_connect_args(url: str) -> dict:
    if make_url(url).get_backend_name() == "postgresql":
        return {"options": "-c timezone=UTC"}
    return {}

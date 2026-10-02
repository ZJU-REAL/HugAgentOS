"""HTTP rate limiter configuration. Circuit state lives in circuit_breaker."""

from core.config.settings import settings
from slowapi import Limiter
from slowapi.util import get_remote_address


def get_rate_limit_enabled() -> bool:
    return settings.rate_limit.enabled


def get_rate_limit_storage() -> str:
    return settings.rate_limit.storage


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_rate_limit_storage(),
    enabled=get_rate_limit_enabled(),
    headers_enabled=True,
)

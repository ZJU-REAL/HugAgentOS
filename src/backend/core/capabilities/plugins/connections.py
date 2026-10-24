"""Public account connection types; credentials never belong in plugin definitions."""
from typing import Any

CONNECTION_TYPES = frozenset({"dingtalk", "lark", "email", "yida"})


def connection_type(value: Any) -> str | None:
    """Accept only the providers with a supported account connection UI."""
    return value if isinstance(value, str) and value in CONNECTION_TYPES else None


def manifest_connection(manifest: dict) -> str | None:
    from core.capabilities.plugins.packaging.importer import ext_or_top as _ext_or_top, manifest_extensions

    return connection_type(_ext_or_top(manifest, manifest_extensions(manifest), "connection"))

"""Community-edition middleware and capability facade."""


def setup_edition_middleware(app) -> None:
    return None


def edition_router_dependencies(policy_key):
    return None


def edition_probe_payload() -> dict:
    return {"edition": "ce"}


def edition_only_route(route_decorator):
    """Discard an enterprise route decorator without registering the handler."""

    def _identity(handler):
        return handler

    return _identity


__all__ = [
    "edition_probe_payload",
    "edition_only_route",
    "edition_router_dependencies",
    "setup_edition_middleware",
]

"""Optional execution-device catalog projected into domain selection."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, Any


class DeviceCatalog(Protocol):
    def account_plugin_installations(self, user_id): ...
    def owned_identities(self, user_id): ...
    def resolve_plugin(self, slug, user_id, *, install_id=None): ...
    def active(self) -> bool: ...
    def agent_entries(self) -> list[dict]: ...
    def plugin_entries(self, user_id=None) -> list[dict]: ...
    def catalog_overlay(self, user_id=None) -> dict: ...
    def installations(self, kind: str, user_id=None) -> list[Any]: ...
    def merge_items(self, base, projected, *, key): ...


@dataclass
class CatalogRegistration:
    provider: DeviceCatalog | None = None


@lru_cache(maxsize=1)
def catalog_registration() -> CatalogRegistration:
    return CatalogRegistration()


def register_device_catalog(provider: DeviceCatalog) -> None:
    catalog_registration().provider = provider


def active() -> bool:
    provider = catalog_registration().provider
    return bool(provider and provider.active())


def agent_entries():
    provider = catalog_registration().provider
    return provider.agent_entries() if provider else []


def plugin_entries(user_id=None):
    provider = catalog_registration().provider
    return provider.plugin_entries(user_id) if provider else []


def catalog_overlay(user_id=None):
    provider = catalog_registration().provider
    return provider.catalog_overlay(user_id) if provider else {}


def installations(kind, user_id=None):
    provider = catalog_registration().provider
    return provider.installations(kind, user_id=user_id) if provider else []


def merge_items(base, projected, *, key):
    provider = catalog_registration().provider
    if provider is None:
        if projected:
            raise RuntimeError("Device catalog is not registered")
        return base
    return provider.merge_items(base, projected, key=key)


def resolve_plugin(slug, user_id, *, install_id=None):
    provider = catalog_registration().provider
    return provider.resolve_plugin(slug, user_id, install_id=install_id) if provider else None


def account_plugin_installations(user_id):
    provider = catalog_registration().provider
    return provider.account_plugin_installations(user_id) if provider else []


def owned_identities(user_id):
    provider = catalog_registration().provider
    return provider.owned_identities(user_id) if provider else set()

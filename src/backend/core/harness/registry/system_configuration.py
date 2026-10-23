"""Persistence contract for the configuration cache."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol


class SystemConfigurationStore(Protocol):
    def set(self, key: str, value: str | None, updated_by: str): ...
    def bulk_set(self, items: list[dict], updated_by: str): ...
    def load(self) -> tuple[dict, dict]: ...


@dataclass
class ConfigurationRegistration:
    store: SystemConfigurationStore | None = None


@lru_cache(maxsize=1)
def configuration_registration() -> ConfigurationRegistration:
    return ConfigurationRegistration()


def register_system_configuration_store(store: SystemConfigurationStore) -> None:
    configuration_registration().store = store


def system_configuration_store() -> SystemConfigurationStore:
    store = configuration_registration().store
    if store is None:
        raise RuntimeError("System configuration persistence is not registered")
    return store

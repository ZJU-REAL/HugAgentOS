"""Source and upgrade contracts for plugin packages."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol


class PackageSource(Protocol):
    def normalize(self, db: Any, slug: str) -> Any: ...
    def archive(self, db: Any, slug: str) -> bytes | None: ...


class PackageUpgradePolicy(Protocol):
    def lock_configuration(self, db: Any) -> None: ...
    def prepare(self, db: Any, existing: Any, package: Any, source: str, approved_revision: str | None) -> Any: ...
    def reserve_local(self, existing: Any, revision: str, files: Any) -> None: ...
    def prepare_local(self, db: Any, current: Any, prepared: Any) -> Any: ...


def register_package_source(name: str, source: PackageSource) -> None:
    registration_state().sources[name] = source


def package_source(name: str) -> PackageSource:
    return registration_state().sources[name]


def register_package_upgrade_policy(policy: PackageUpgradePolicy) -> None:
    pass
    registration_state().upgrade_policy = policy


def package_upgrade_policy() -> PackageUpgradePolicy:
    if registration_state().upgrade_policy is None:
        raise RuntimeError("Host must register the package upgrade policy before installing plugins")
    return registration_state().upgrade_policy


@dataclass
class RegistrationState:
    sources: dict[str, PackageSource] = field(default_factory=dict)
    upgrade_policy: PackageUpgradePolicy | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS

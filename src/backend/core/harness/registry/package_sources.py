"""Source and upgrade contracts for plugin packages."""
from __future__ import annotations
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


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    sources: dict[str, PackageSource] = state_field(default_factory=dict)
    upgrade_policy: PackageUpgradePolicy | None = None


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()

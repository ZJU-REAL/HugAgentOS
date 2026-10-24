"""Host-owned file grants and recovery snapshots used by core operations."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, Any


class LocalSecurityProvider(Protocol):
    def grants_for_gate(self) -> list[Any]: ...
    def policy_for_gate(self, approval_mode: str) -> Any: ...


class LocalSnapshotProvider(Protocol):
    def snapshot(self, real_path: str) -> str | None: ...
    def maybe_snapshot_local(self, real_path: str) -> str | None: ...


@dataclass
class LocalFileServices:
    security: LocalSecurityProvider | None = None
    snapshots: LocalSnapshotProvider | None = None


@lru_cache(maxsize=1)
def local_file_services() -> LocalFileServices:
    return LocalFileServices()


def register_local_file_services(security: LocalSecurityProvider,
                                 snapshots: LocalSnapshotProvider) -> None:
    services = local_file_services()
    services.security = security
    services.snapshots = snapshots


def grants_for_gate() -> list[Any]:
    provider = local_file_services().security
    if provider is None:
        raise RuntimeError("Local file security provider is not registered")
    return provider.grants_for_gate()


def policy_for_gate(approval_mode: str) -> Any:
    provider = local_file_services().security
    if provider is None:
        raise RuntimeError("Local file security provider is not registered")
    return provider.policy_for_gate(approval_mode)


def snapshot(real_path: str) -> str | None:
    provider = local_file_services().snapshots
    if provider is None:
        raise RuntimeError("Local file snapshot provider is not registered")
    return provider.snapshot(real_path)


def maybe_snapshot_local(real_path: str) -> str | None:
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled():
        return None
    provider = local_file_services().snapshots
    if provider is None:
        raise RuntimeError("Local file snapshot provider is not registered")
    return provider.maybe_snapshot_local(real_path)

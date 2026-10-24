"""Authorized capability revision snapshots supplied by the installed host."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol


class CapabilityRevisions(Protocol):
    def model(self, db, kind, key): ...
    def snapshot_in_session(self, db, user, kind, key): ...
    def snapshot(self, user, kind, key): ...
    def commit(self, user, kind, key, body): ...
    def revision(self, files): ...
    def record_id(self, prefix, user, kind, key): ...


@dataclass
class RevisionRegistration:
    provider: CapabilityRevisions | None = None


@lru_cache(maxsize=1)
def revision_registration() -> RevisionRegistration:
    return RevisionRegistration()


def register_capability_revisions(provider: CapabilityRevisions) -> None:
    revision_registration().provider = provider


def capability_revisions() -> CapabilityRevisions:
    provider = revision_registration().provider
    if provider is None:
        raise RuntimeError("Capability revision service is not registered")
    return provider

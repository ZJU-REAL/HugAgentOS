"""Process-local ordering for remote manifest requests and short publications.

A newer request supersedes older in-flight responses even if it later fails.
Callers acquire account_scope before apply; neither scope may contain network IO.
Tickets are Python attributes, never manifest fields or public revision inputs.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from .errors import CloudUnavailable


class StaleManifest(CloudUnavailable):
    def __init__(self):
        super().__init__("cloud manifest response was superseded; retry current authorization")


@dataclass(frozen=True)
class Ticket:
    kind: str
    profile: str
    sequence: int


class Snapshot(dict):
    def __init__(self, manifest, ticket):
        super().__init__(manifest)
        self._manifest_ticket = ticket


_lock = threading.RLock()
_latest = {}


def begin(kind, profile):
    with _lock:
        key = (kind, profile)
        sequence = _latest.get(key, 0) + 1
        _latest[key] = sequence
        return Ticket(kind, profile, sequence)


def stamp(manifest, ticket):
    return Snapshot(manifest, ticket)


@contextmanager
def apply(kind, profile, manifest):
    with _lock:
        ticket = getattr(manifest, "_manifest_ticket", None)
        if (
            not isinstance(ticket, Ticket)
            or (ticket.kind, ticket.profile) != (kind, profile)
            or _latest.get((kind, profile)) != ticket.sequence
        ):
            raise StaleManifest()
        yield

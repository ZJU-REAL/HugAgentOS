"""Normalize native events without losing rename pairs or accepting read events."""

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Change:
    kind: str
    source: str
    destination: str | None = None
    directory: bool = False
    observed: float = 0

    @property
    def path(self):
        return self.destination or self.source

    def paths(self):
        return tuple(dict.fromkeys(p for p in (self.source, self.destination) if p))


def normalize(event, root, excluded):
    if event.event_type not in {"created", "modified", "closed", "deleted", "moved"}:
        return None
    if event.is_directory and event.event_type in {"modified", "closed"}:
        return None

    def relative(raw):
        if not raw:
            return None
        try:
            path = Path(raw).relative_to(root)
        except ValueError:
            return None
        if not path.parts or excluded.intersection(path.parts):
            return None
        return path.as_posix()

    source, destination = relative(event.src_path), relative(getattr(event, "dest_path", None))
    if source is None and destination is None:
        return None
    kind = event.event_type
    if source is None:
        kind, source, destination = "created", destination, None
    elif kind == "moved" and destination is None:
        kind = "deleted"
    return Change(kind, source, destination, event.is_directory, time.time())


def rebase_move(change, parent):
    """Rebase a real child rename; collapse the observer's synthetic parent move."""
    if change.kind != "moved":
        return change

    def remap(path):
        if path and path.startswith(parent.source + "/"):
            return parent.destination + path[len(parent.source) :]
        return path

    source, destination = remap(change.source), remap(change.destination)
    if source == change.destination and source != change.source:
        return Change("modified", source, directory=change.directory, observed=change.observed)
    if source != change.source or destination != change.destination:
        return Change("moved", source, destination, change.directory, change.observed)
    return change

"""Small recursive immutable JSON-value helpers.

These types are used for content-addressed evidence.  A frozen dataclass alone
does not protect nested dictionaries/lists, so leaving those mutable would let
callers change the payload without changing its hash-derived identifier.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


class FrozenDict(Mapping[str, Any]):
    """An immutable mapping whose nested JSON containers are frozen too."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        self._data = {str(key): freeze_json(item) for key, item in (value or {}).items()}

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False


def freeze_json(value: Any) -> Any:
    """Recursively freeze dictionaries and sequence containers."""
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return a detached mutable JSON-compatible representation."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [thaw_json(item) for item in value]
    return value


__all__ = ["FrozenDict", "freeze_json", "thaw_json"]

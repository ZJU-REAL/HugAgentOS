"""Projection functions supplied by the event log's owning domain."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProjectionCodec:
    new_projection: Callable[[str], dict[str, Any]]
    fold_projection: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    affected_tools: Callable[[dict[str, Any], dict[str, Any]], set[int]]
    encode_checkpoint: Callable[..., dict[str, str]]
    decode_checkpoint: Callable[..., dict[str, Any] | None]

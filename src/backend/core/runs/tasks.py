"""Local worker handles, keyed by durable run identity."""

from __future__ import annotations

import asyncio
import typing as _typing
from dataclasses import dataclass, field

from core.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TasksState:
    active_runs: _typing.Dict[str, asyncio.Task] = field(default_factory=lambda: {})


_STATE = TasksState()


def process_state() -> TasksState:
    return _STATE

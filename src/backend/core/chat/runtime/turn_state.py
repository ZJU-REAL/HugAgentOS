"""State owned by one chat worker; persistence callbacks share this exact turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from core.chat.tool_log import SegmentRecorder
    from core.runs.events.writer import EventOffsets
    from core.runs.journal import RunJournal


@dataclass
class TurnState:
    chat_id: str
    context: dict[str, Any]
    message_id: str
    model_name: str
    run_id: str
    user_id: str
    stopped: bool = False
    inflight_interval_s: float = 2.0
    emit: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
    flush_lock: asyncio.Lock | None = None
    flushed_snapshot_key: Any = None
    model_steps: list[dict[str, Any]] = field(default_factory=list)
    round_block: dict[str, Any] | None = None
    run_started_monotonic: float = 0.0
    segments: SegmentRecorder | None = None
    thinking_log: list[dict[str, Any]] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    compaction_budget_inputs: dict[str, Any] = field(default_factory=dict)
    current_chat_seq: int | None = None
    current_message_id: str = ""
    full_response: str = ""
    journal: RunJournal | None = None
    latest_user_message: str = ""
    lease_task: asyncio.Task | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    offsets: EventOffsets | None = None
    owner: str = ""
    tool_calls_log: list[dict[str, Any]] = field(default_factory=list)

"""Existing run kinds, statuses and execution deadlines shared by run owners."""

from __future__ import annotations

import os
import socket
import typing as _typing
import uuid

import core.llm.runtime.human_interaction as human_interaction
from core.infra.logging import get_logger

logger = get_logger(__name__)

RunKind = _typing.Literal["chat", "plan_execute", "plan_generate", "autonomous_loop"]


_COOPERATIVE_KINDS: tuple[str, ...] = ("plan_execute", "autonomous_loop")


RunStatus = _typing.Literal[
    "pending", "running", "needs_attention", "completed", "failed", "cancelled"
]


_TERMINAL_STATUSES: tuple[RunStatus, ...] = (
    "needs_attention",
    "completed",
    "failed",
    "cancelled",
)


LIVE_STATUSES: tuple[RunStatus, ...] = ("pending", "running")


_STREAM_TTL_SECONDS = human_interaction.STREAM_TTL_SECONDS


TERMINAL_TYPE = "__terminal__"


_XREAD_BLOCK_MS = 5000


HEARTBEAT_INTERVAL_SEC = 15.0


_FIRST_RESPONSE_TIMEOUT_SEC = 600.0


_INACTIVITY_TIMEOUT_SEC = float(os.getenv("CHAT_RUN_INACTIVITY_TIMEOUT_SEC", "600"))


_STALE_RUN_MAX_AGE_SEC = float(os.getenv("CHAT_RUN_MAX_AGE_SEC", "1800"))


STALE_REAPER_INTERVAL_SEC = float(os.getenv("CHAT_RUN_REAPER_INTERVAL_SEC", "300"))


_STALE_QUIET_SEC = float(os.getenv("CHAT_RUN_STALE_QUIET_SEC", str(_INACTIVITY_TIMEOUT_SEC)))


_HARD_MAX_AGE_SEC = float(os.getenv("CHAT_RUN_HARD_MAX_AGE_SEC", "21600"))


RUN_LEASE_SECONDS = max(15, int(os.getenv("CHAT_RUN_LEASE_SECONDS", "90")))


_RUN_LEASE_HEARTBEAT_SEC = max(1.0, RUN_LEASE_SECONDS / 3)


RUN_RECOVERY_INTERVAL_SEC = max(
    1.0,
    float(os.getenv("CHAT_RUN_RECOVERY_INTERVAL_SEC", "15")),
)


_WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


_OFFSET_RESERVATION = 64


_CANCEL_VISIBILITY_SEC = 0.25


_BACKGROUND_RUN_KINDS = frozenset(
    {
        "automation_plan",
        "automation_prompt",
        "internal_job_agent",
        "legacy_chat_stream",
    }
)


MAX_RECENT_RUN_SECONDS = 86400

"""State shared only by the main agent execution phases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from core.agent_runtime.integrations.memory_session import SessionMemory
    from core.llm.citations.allocator import AnchorAllocator
    from core.llm.runtime.streaming import StreamingAgent


@dataclass
class MainTurn:
    context: dict[str, Any]
    session_messages: list[dict[str, Any]]
    user_message: str
    closed: bool = False
    review_task: asyncio.Task | None = None
    review_owner: str | None = None
    anchor_allocator: AnchorAllocator | None = None
    ctx_window: int = 0
    last_plan: dict[str, Any] | None = None
    main_stream: AsyncIterator | None = None
    memory_enabled: bool = False
    memory_user_id: str = ""
    memory_workspace_id: str = "default"
    mentioned_ids: list[str] = field(default_factory=list)
    ontology_event_cursor: int = 0
    ontology_runtime: dict[str, Any] = field(default_factory=dict)
    ontology_trace: list[dict[str, Any]] = field(default_factory=list)
    pending_plan_updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_memory: SessionMemory | None = None
    visible_subagents: list[dict[str, Any]] = field(default_factory=list)
    agent: Any = None
    all_citations: list[dict[str, Any]] = field(default_factory=list)
    displayed_tools: set[str] = field(default_factory=set)
    full_response: str = ""
    mcp_clients: list[Any] = field(default_factory=list)
    skill_id_by_tool_id: dict[str, str] = field(default_factory=dict)
    skill_load_ids: set[str] = field(default_factory=set)
    streaming_agent: StreamingAgent | None = None
    view_text_file_args: dict[str, dict[str, Any]] = field(default_factory=dict)

"""call_subagent tool — allows the main agent to dispatch tasks to sub-agents.

Each sub-agent runs in an isolated thread with its own event loop to avoid
anyio cancel-scope cross-task errors from MCP clients.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from core.llm.context.rendering import next_request_sequence, render_context_item
from core.llm.context.ir_constants import KIND_USER_INPUT, POLICY_NEVER
from core.llm.context.items import make_text_context_item
from core.llm.tools._common import resolve_sandbox_session
from core.db.threads import run_in_session

logger = logging.getLogger(__name__)


class _ChildExecution:
    """Bridge cancellation to the task that owns the child's MCP scopes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._owner = None

    def bind(self, loop, task) -> None:
        with self._lock:
            self._owner = (loop, task)
            if self._cancelled:
                task.cancel()

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            if self._owner is not None:
                loop, task = self._owner
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)

    def unbind(self) -> None:
        with self._lock:
            self._owner = None


def _shared_ontology_runtime(agent_ref: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the parent request's ontology runtime without copying it.

    A child agent may activate an additional asset-tag workflow or raise the
    required review level. Keeping the same dictionary object makes those
    monotonic policy changes visible to the outer workflow, which remains the
    sole owner of final-answer review.
    """
    if not agent_ref or not agent_ref.get("agent"):
        return None
    runtime = getattr(agent_ref["agent"].state, "ontology_runtime", None)
    return runtime if isinstance(runtime, dict) else None


class _SubMapper:
    """Adapt the main stream's normalized events to sidebar sub-steps."""

    def __init__(self) -> None:
        self._structured = False
        self._think_buf = ""
        self._closed = False

    def _flush_pending(self) -> List[Dict[str, Any]]:
        text = self._think_buf
        self._think_buf = ""
        return [{"sub_type": "content", "delta": text}] if text else []

    def finish_turn(self) -> List[Dict[str, Any]]:
        out = self._flush_pending()
        self._closed = self._structured
        return out

    def feed(self, kind: str, payload: Any) -> List[Dict[str, Any]]:
        if kind == "reasoning_protocol":
            self._structured = bool(payload.get("structured_reasoning"))
            self._closed = self._structured
            return self._flush_pending() if self._structured else []
        if kind == "text_delta":
            delta = payload or ""
            if not delta:
                return []
            if self._closed:
                delta = delta.replace("<think>", "").replace("</think>", "")
                return [{"sub_type": "content", "delta": delta}] if delta else []
            self._think_buf += delta
            close_i = self._think_buf.find("</think>")
            if close_i < 0:
                return []
            thinking = self._think_buf[:close_i].replace("<think>", "")
            rest = self._think_buf[close_i + len("</think>") :]
            self._think_buf = ""
            self._closed = True
            out = []
            if thinking.strip():
                out.append({"sub_type": "thinking", "delta": thinking})
            if rest:
                out.append({"sub_type": "content", "delta": rest})
            return out
        if kind == "thinking_delta":
            self._closed = True
            return [{"sub_type": "thinking", "delta": payload}] if payload else []
        if kind == "tool_call_start":
            return [
                {
                    "sub_type": "tool_call",
                    "tool_id": payload["id"],
                    "tool_name": payload["name"],
                    "input": None,
                    "status": "running",
                }
            ]
        if kind == "tool_call_delta":
            return [
                {
                    "sub_type": "tool_call_delta",
                    "tool_id": payload["id"],
                    "tool_name": payload["name"],
                    "arguments_delta": payload["delta"],
                }
            ]
        if kind == "tool_call":
            return [
                {
                    "sub_type": "tool_call",
                    "tool_id": payload["id"],
                    "tool_name": payload["name"],
                    "input": payload["args"],
                    "status": "running",
                }
            ]
        if kind == "tool_result":
            return [
                {
                    "sub_type": "tool_result",
                    "tool_id": payload["id"],
                    "tool_name": payload["name"],
                    "output": payload["content"],
                    "status": (
                        "error"
                        if payload["status"] in {"error", "denied", "interrupted"}
                        else "success"
                    ),
                }
            ]
        return []


async def _map_subagent_event(
    stream_mapper: Any, mapper: _SubMapper, chunk: Any
) -> List[Dict[str, Any]]:
    subs: List[Dict[str, Any]] = []
    protocol = stream_mapper._take_reasoning_protocol()
    if protocol:
        subs.extend(mapper.feed("reasoning_protocol", protocol))
    async for kind, payload in stream_mapper._map_event(chunk):
        subs.extend(mapper.feed(kind, payload))
    if type(chunk).__name__ == "ModelCallEndEvent":
        subs.extend(mapper.finish_turn())
    return subs


def _run_subagent_in_thread(
    agent_id: str,
    agent_name: str,
    task: str,
    context_summary: str,
    current_user_id: str,
    shared_messages: Optional[List[Dict[str, Any]]] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ontology_runtime: Optional[Dict[str, Any]] = None,
    parent_runtime: Optional[Dict[str, Any]] = None,
    resume_messages: Optional[List[Dict[str, Any]]] = None,
    execution: Optional[_ChildExecution] = None,
) -> Tuple[bool, str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run a single sub-agent inside a *new* event loop on a worker thread.

    Returns ``(True, response_text, pinned_files, final_messages)`` on success,
    ``(False, error_message, [], [])`` on failure.

    ``final_messages`` is the child's context after the run; the caller persists
    it so a follow-up dispatch can resume this same child instead of starting
    over. ``resume_messages`` is such a context from an earlier run — it
    replaces shared-context inheritance, which it already contains.

    ``pinned_files`` are the files
    the sub-agent added via ``pin_to_workspace`` (get_pinned() shape) — the
    sub-agent runs in its own thread/event-loop and therefore its own workspace
    ContextVar, so these must be handed back and re-pinned in the main context
    or they never reach the user-visible assistant message.

    When ``emit`` is provided, the sub-agent is consumed **in streaming mode**
    (``agent.reply_stream``, event by event) and every thinking/tool_call/tool_result/content
    sub-step is bypass-forwarded back to the main SSE stream via ``emit``; with ``emit``
    None the behavior matches the old implementation (final text only). The returned text
    is always taken from the reply's **final Msg** — fully equivalent to the old
    ``agent.reply()``, leaving the tool result the main agent receives unchanged.
    """

    async def _inner() -> str:
        from agentscope.message import Msg
        from core.capabilities.agents.builtin import build_builtin_runtime_profile, get_builtin_subagent
        from core.llm.factory import create_agent_executor
        from core.llm.mcp.manager import close_clients
        from core.llm.runtime.message_compat import extract_messages_from_context, session_to_msgs
        from core.llm.runtime.streaming import StreamingAgent
        from core.capabilities.agents.edition import UserAgentService

        builtin_spec = get_builtin_subagent(agent_id)
        if builtin_spec is not None:
            user_agent = build_builtin_runtime_profile(builtin_spec, parent_runtime)
        else:
            def _load_user_agent(db):
                svc = UserAgentService(db)
                user_agent = svc.get_raw_by_id(agent_id, user_id=current_user_id)
                _ = user_agent.mcp_server_ids, user_agent.skill_ids, user_agent.kb_ids
                _ = user_agent.system_prompt, user_agent.model_provider_id
                _ = (
                    user_agent.max_iters,
                    user_agent.temperature,
                    user_agent.max_tokens,
                    user_agent.timeout,
                )
                return user_agent
            user_agent = await run_in_session(_load_user_agent)

        # A conversation is the only sandbox boundary. Every built-in and user-defined
        # child agent inherits the parent's sandbox session and therefore sees the same
        # live workspace. Conversation/context inheritance remains an orthogonal policy
        # controlled by shared_messages below.
        runtime = parent_runtime or {}
        sub_session_id = resolve_sandbox_session(
            runtime.get("sandbox_session_id"),
            runtime.get("chat_id"),
        )
        from core.projects.local_workspace import child_project_context

        agent, mcp_clients = await create_agent_executor(
            user_agent=user_agent,
            current_user_id=current_user_id,
            isolated=True,
            sandbox_session_id=sub_session_id,
            chat_id=runtime.get("chat_id") if builtin_spec is not None else None,
            run_id=runtime.get("run_id"),
            journal_owner=runtime.get("journal_owner"),
            capability_scope=str(runtime.get("capability_scope") or ""),
            project_ctx=child_project_context(runtime, builtin=builtin_spec is not None),
            channel_origin=runtime.get("channel_origin") if builtin_spec is not None else None,
            automation_run=bool(runtime.get("automation_run")),
            reranker_enabled=bool(runtime.get("reranker_enabled")),
            model_name=runtime.get("model_name"),
            model_provider_id=runtime.get("model_provider_id"),
            chat_mode=runtime.get("chat_mode"),
            ontology_runtime=ontology_runtime,
            approval_mode=runtime.get("approval_mode"),
        )
        # The sub-agent runs in its own thread/event loop → its own workspace ContextVar.
        # Initialize one so the sub-agent's pin_to_workspace has somewhere to land; after the
        # run, get_pinned() hands the files back to the main context for re-pinning —
        # otherwise files pinned to the workspace are lost and never shown in the main conversation.
        from core.llm.runtime import workspace as _ws

        _ws.init_state()

        try:
            # Load the shared context into the sub-agent (2.0: agent.memory → agent.state.context).
            # A resumed run brings its own context forward, which already contains
            # whatever it inherited the first time.
            if resume_messages:
                agent.state.context.extend(session_to_msgs(resume_messages))
            elif shared_messages:
                agent.state.context.extend(session_to_msgs(shared_messages))

            prompt_parts = []
            if context_summary:
                prompt_parts.append(f"对话背景：{context_summary}")
            prompt_parts.append(f"用户任务：{task}")
            prompt = "\n\n".join(prompt_parts)

            request_seq = next_request_sequence(agent.state.context)
            user_msg = render_context_item(
                make_text_context_item(
                    prompt,
                    item_id=f"subagent:delegated_task:{request_seq}",
                    kind=KIND_USER_INPUT,
                    origin="user:delegated_task",
                    trust="user",
                    created_seq=request_seq,
                    priority=1_000,
                    token_budget=100_000,
                    truncation_policy=POLICY_NEVER,
                )
            )

            if emit is None:
                # No listener (non-interactive / batch / main stream not registered): take the original one-shot path.
                result = await agent.reply(inputs=user_msg)
                response_text = result.get_text_content() or ""
                return (
                    True,
                    response_text,
                    _ws.get_pinned(),
                    extract_messages_from_context(agent.state.context),
                )

            # Public stream includes the final message for child result extraction.
            final_msg = None
            mapper = _SubMapper()
            stream_mapper = StreamingAgent(agent, mcp_clients)
            stream_mapper._enable_thinking = True
            _last_progress = time.monotonic()
            async for chunk in agent.reply_stream(inputs=user_msg, yield_final_msg=True):
                if isinstance(chunk, Msg):
                    final_msg = chunk
                    continue
                try:
                    subs = await _map_subagent_event(stream_mapper, mapper, chunk)
                    for sub in subs:
                        emit(sub)
                    if subs:
                        _last_progress = time.monotonic()
                    else:
                        _now = time.monotonic()
                        if _now - _last_progress >= 5.0:
                            _last_progress = _now
                            emit({"sub_type": "progress"})
                except (
                    Exception
                ):  # noqa: BLE001 — bypass mapping must never take down the sub-agent
                    logger.debug("subagent event map failed (ignored)", exc_info=True)
            for kind, payload in stream_mapper._deltas.drain():
                for sub in mapper.feed(kind, payload):
                    emit(sub)
            response_text = (final_msg.get_text_content() if final_msg else "") or ""
            return (
                True,
                response_text,
                _ws.get_pinned(),
                extract_messages_from_context(agent.state.context),
            )
        finally:
            # Child agents share the parent conversation sandbox. Its lifecycle is owned
            # by the conversation/session manager, never by an individual child run.
            try:
                await close_clients(mcp_clients)
            except BaseException as exc:
                logger.debug("close_clients error (ignored): %s", exc)

    async def _owned_inner():
        if execution is not None:
            execution.bind(asyncio.get_running_loop(), asyncio.current_task())
        try:
            await asyncio.sleep(0)  # deliver cancellation before construction starts
            return await _inner()
        finally:
            from core.infra.redis import close_redis
            from core.models.chat_models import close_loop_http_clients
            from core.llm.runtime.model_cache import release_loop_models

            release_loop_models()
            try:
                await close_loop_http_clients()
            finally:
                try:
                    await close_redis()
                finally:
                    if execution is not None:
                        execution.unbind()

    try:
        return asyncio.run(_owned_inner())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("subagent thread failed: agent=%s, error=%s", agent_name, e, exc_info=True)
        return False, str(e)[:200], [], []

"""call_subagent tool — allows the main agent to dispatch tasks to sub-agents.

Each sub-agent runs in an isolated thread with its own event loop to avoid
anyio cancel-scope cross-task errors from MCP clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentscope.message import TextBlock
from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from core.services import log_service as log_writer

logger = logging.getLogger(__name__)

# Thread pool for sub-agent execution.
# Each thread gets its own event loop so anyio cancel scopes stay within
# a single task — avoiding the cross-task RuntimeError.
_subagent_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="subagent")


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
    """Accumulates/maps the sub-agent reply_stream's fine-grained events into frontend-renderable sub-step dicts.

    Isomorphic to orchestration/streaming.py::StreamingAgent._map_event (a tool_call's
    name/args are spread across Start/Delta/End, and tool_result text across multiple
    Deltas), but does **not** perform side effects such as tool_log persistence —
    pure mapping, for bypass pass-through.
    """

    def __init__(self) -> None:
        self._names: Dict[str, str] = {}  # tool_id → name
        self._args: Dict[str, str] = {}  # tool_id → accumulated args JSON string
        self._results: Dict[str, str] = {}  # tool_id → accumulated result text
        # Inline thinking (<think>…</think>) splitting: the deepseek/qwen family models
        # commonly used by sub-agents inline the reasoning chain in the body deltas, and
        # often omit the opening <think>. Buffer the leading text per "model turn" —
        # seeing </think> confirms that segment is thinking (→ thinking sub-step, rendered
        # as a "thinking" module) and what follows is the answer (→ content, emitted
        # directly); if </think> never appears in the whole turn, it's a non-thinking
        # model's plain answer, re-emitted as content at turn end. Structured
        # ThinkingBlockDeltaEvent likewise goes through the thinking channel. State resets
        # on each ModelCall turn.
        self._think_buf = ""  # pending text before </think> in this turn
        self._closed = False  # whether thinking is confirmed finished this turn (saw </think> or structured thinking)

    def _flush_pending_as_content(self, out: List[Dict[str, Any]]) -> None:
        if self._think_buf:
            out.append({"sub_type": "content", "delta": self._think_buf})
            self._think_buf = ""

    def feed(self, ev: Any) -> List[Dict[str, Any]]:
        nm = type(ev).__name__
        out: List[Dict[str, Any]] = []

        if nm == "TextBlockDeltaEvent":
            d = getattr(ev, "delta", "") or ""
            if not d:
                return out
            if self._closed:
                # Thinking already ended (or this model doesn't inline thinking) → body answer; emit directly and strip leftover tags
                d = d.replace("<think>", "").replace("</think>", "")
                if d:
                    out.append({"sub_type": "content", "delta": d})
                return out
            # Unconfirmed: buffer the leading text, wait for </think> to classify it
            self._think_buf += d
            close_i = self._think_buf.find("</think>")
            if close_i != -1:
                think_txt = self._think_buf[:close_i].replace("<think>", "")
                rest = self._think_buf[close_i + len("</think>") :]
                self._think_buf = ""
                self._closed = True
                if think_txt.strip():
                    out.append({"sub_type": "thinking", "delta": think_txt})
                if rest:
                    out.append({"sub_type": "content", "delta": rest})
            return out

        elif nm == "ThinkingBlockDeltaEvent":
            # Structured thinking: goes straight through the thinking channel; also marks that body text from here on this turn is the answer (emitted directly).
            d = getattr(ev, "delta", "") or ""
            self._closed = True
            if d:
                out.append({"sub_type": "thinking", "delta": d})

        elif nm == "ModelCallEndEvent":
            # Turn end: buffered text with no </think> seen all turn = a non-thinking model's answer → re-emit as content.
            self._flush_pending_as_content(out)
            self._closed = False

        elif nm == "ToolCallStartEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            name = getattr(ev, "tool_call_name", "") or "unknown"
            self._names[tid] = name
            self._args[tid] = ""
            out.append(
                {
                    "sub_type": "tool_call",
                    "tool_id": tid,
                    "tool_name": name,
                    "input": None,
                    "status": "running",
                }
            )

        elif nm == "ToolCallDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            self._args[tid] = self._args.get(tid, "") + (getattr(ev, "delta", "") or "")

        elif nm == "ToolCallEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            name = self._names.get(tid, "unknown")
            args_str = self._args.get(tid, "")
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {"_raw": args_str}
            out.append(
                {
                    "sub_type": "tool_call",
                    "tool_id": tid,
                    "tool_name": name,
                    "input": args,
                    "status": "running",
                }
            )

        elif nm == "ToolResultTextDeltaEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            self._results[tid] = self._results.get(tid, "") + (getattr(ev, "delta", "") or "")

        elif nm == "ToolResultEndEvent":
            tid = getattr(ev, "tool_call_id", "") or ""
            content = self._results.pop(tid, "")
            name = getattr(ev, "tool_call_name", "") or self._names.get(tid, "unknown")
            state = str(getattr(ev, "state", "") or "")
            out.append(
                {
                    "sub_type": "tool_result",
                    "tool_id": tid,
                    "tool_name": name,
                    "output": content,
                    "status": "error" if state == "error" else "success",
                }
            )

        return out


def _run_subagent_in_thread(
    agent_id: str,
    agent_name: str,
    task: str,
    context_summary: str,
    current_user_id: str,
    shared_messages: Optional[List[Dict[str, Any]]] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ontology_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Run a single sub-agent inside a *new* event loop on a worker thread.

    Returns ``(True, response_text, pinned_files)`` on success,
    ``(False, error_message, [])`` on failure. ``pinned_files`` are the files
    the sub-agent added via ``pin_to_workspace`` (get_pinned() shape) — the
    sub-agent runs in its own thread/event-loop and therefore its own workspace
    ContextVar, so these must be handed back and re-pinned in the main context
    or they never reach the user-visible assistant message.

    When ``emit`` is provided, the sub-agent is consumed **in streaming mode**
    (``agent._reply``, event by event) and every thinking/tool_call/tool_result/content
    sub-step is bypass-forwarded back to the main SSE stream via ``emit``; with ``emit``
    None the behavior matches the old implementation (final text only). The returned text
    is always taken from the reply's **final Msg** — fully equivalent to the old
    ``agent.reply()``, leaving the tool result the main agent receives unchanged.
    """

    async def _inner() -> str:
        from agentscope.message import Msg
        from core.db.engine import SessionLocal
        from core.llm.agent_factory import create_agent_executor
        from core.llm.mcp_manager import close_clients
        from core.llm.message_compat import session_to_msgs, strip_thinking
        from core.services.user_agent_service import UserAgentService

        with SessionLocal() as db:
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

        # The sub-agent uses an "independent user-bound persistent sandbox" — the same
        # _create_jupyter_sandbox path as the main conversation, mounting that user's
        # credential volumes (lark/dws/email/myspace) so Feishu/DingTalk/email CLI plugins
        # get credentials inside the sub-agent too. A unique session id isolates it from
        # the main conversation and concurrent sub-agents (no shared container state);
        # explicitly destroyed in finally after the run to avoid sandbox pile-up.
        # (The old implementation passed sandbox_session_id="" which went through the
        #  ephemeral light pool — a credential-less sandbox, and exactly the root cause of
        #  lark-cli reporting "not configured" inside sub-agents.)
        sub_session_id = f"sub-{agent_id}-{uuid.uuid4().hex[:12]}"
        agent, mcp_clients = await create_agent_executor(
            user_agent=user_agent,
            current_user_id=current_user_id,
            isolated=True,
            sandbox_session_id=sub_session_id,
            ontology_runtime=ontology_runtime,
        )
        # The sub-agent runs in its own thread/event loop → its own workspace ContextVar.
        # Initialize one so the sub-agent's pin_to_workspace has somewhere to land; after the
        # run, get_pinned() hands the files back to the main context for re-pinning —
        # otherwise files pinned to the workspace are lost and never shown in the main conversation.
        from core.llm import workspace as _ws

        _ws.init_state()

        try:
            # Load the shared context into the sub-agent (2.0: agent.memory → agent.state.context)
            if shared_messages:
                agent.state.context.extend(session_to_msgs(shared_messages))

            prompt_parts = []
            if context_summary:
                prompt_parts.append(f"对话背景：{context_summary}")
            prompt_parts.append(f"用户任务：{task}")
            prompt = "\n\n".join(prompt_parts)

            user_msg = Msg(name="user", role="user", content=[TextBlock(type="text", text=prompt)])

            if emit is None:
                # No listener (non-interactive / batch / main stream not registered): take the original one-shot path.
                result = await agent.reply(inputs=user_msg)
                response_text = result.get_text_content() or ""
                return True, strip_thinking(response_text), _ws.get_pinned()

            # Streaming path: consume _reply directly (reply_stream would discard the final
            # Msg, which we need as the authoritative return text). Non-Msg events are mapped
            # to sub-steps and bypass-forwarded via emit.
            final_msg = None
            mapper = _SubMapper()
            async for chunk in agent._reply(inputs=user_msg):
                if isinstance(chunk, Msg):
                    final_msg = chunk
                    continue
                try:
                    for sub in mapper.feed(chunk):
                        emit(sub)
                except (
                    Exception
                ):  # noqa: BLE001 — bypass mapping must never take down the sub-agent
                    logger.debug("subagent event map failed (ignored)", exc_info=True)
            response_text = (final_msg.get_text_content() if final_msg else "") or ""
            return True, strip_thinking(response_text), _ws.get_pinned()
        finally:
            # Destroy the sub-agent's dedicated sandbox session (the unique session is never
            # reused and must be reclaimed explicitly — otherwise every call leaks one
            # persistent sandbox, and relying on the idle reaper as backstop is too slow).
            # close_session is a SandboxProvider protocol method; each provider decides its
            # own semantics (opensandbox/cube destroy; script_runner no-op), so no getattr
            # fallback is needed.
            try:
                from core.sandbox import get_sandbox_provider

                await get_sandbox_provider().close_session(sub_session_id)
            except BaseException as exc:
                logger.debug("subagent close_session error (ignored): %s", exc)
            try:
                await close_clients(mcp_clients)
            except BaseException as exc:
                logger.debug("close_clients error (ignored): %s", exc)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_inner())
    except Exception as e:
        logger.error(
            "subagent thread failed: agent=%s, error=%s",
            agent_name,
            e,
            exc_info=True,
        )
        return False, str(e)[:200], []
    finally:
        loop.close()


def register_subagent_tool(
    toolkit: Toolkit,
    visible_agents: List[Dict[str, Any]],
    current_user_id: str,
    agent_ref: Optional[Dict] = None,
    chat_id: Optional[str] = None,
) -> None:
    """Register the call_subagent tool into the main agent's toolkit.

    Args:
        agent_ref: Mutable container ``{"agent": None}`` — set to the main
            agent instance after creation.  Used to extract shared context
            for sub-agents that have ``extra_config.shared_context == True``.
        chat_id: This run's chat_id — sub-agent streaming events are bypass-routed back
            to the main SSE stream by it (see core/llm/_subagent_stream.py).
    """
    agent_map = {a["agent_id"]: a for a in visible_agents}

    async def call_subagent(
        agent_id: str,
        task: str,
        context_summary: str = "",
    ) -> ToolResponse:
        """调用子智能体执行专业任务。子智能体拥有独立的工具和专业知识。

        子智能体看不到当前对话历史，因此 task 必须包含足够的背景信息。
        像给一个刚加入的同事布置任务一样编写 task：说明要完成什么、为什么、
        已知什么信息、需要回答什么具体问题。
        不要委托理解——不要写"根据你的发现帮我总结"，而是说明具体要分析什么。

        需要并行调用多个子智能体时，在同一轮回复中生成多个 call_subagent 调用，
        系统会自动并行执行。

        Args:
            agent_id (`str`):
                要调用的子智能体 ID（参见系统提示中的可用子智能体列表）。
            task (`str`):
                完整的任务描述。必须包含：要完成什么及为什么、已知的背景信息、
                需要回答的具体问题。简短的命令式指令会导致低质量结果。
            context_summary (`str`):
                当前对话的关键背景摘要（可选），帮助子智能体理解上下文。
                应包含与任务相关的已知事实，而非完整对话记录。

        Returns:
            `ToolResponse`:
                子智能体的执行结果。结果对用户不可见，你需要汇总后呈现给用户。
        """
        if agent_id not in agent_map:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"错误：子智能体 {agent_id} 不存在或无权访问。请检查 ID 是否正确。",
                    )
                ]
            )

        agent_info = agent_map[agent_id]
        agent_name = agent_info.get("name", agent_id)

        # Check whether shared context is enabled
        shared_context = (agent_info.get("extra_config") or {}).get("shared_context", False)
        shared_messages = None
        if shared_context and agent_ref and agent_ref.get("agent"):
            try:
                from core.llm.message_compat import extract_messages_from_context

                # 2.0: agent.memory → agent.state.context; this function is now synchronous
                shared_messages = extract_messages_from_context(agent_ref["agent"].state.context)
                logger.info(
                    "[subagent_tool] shared_context enabled for agent=%s, messages=%d",
                    agent_name,
                    len(shared_messages),
                )
            except Exception as exc:
                logger.warning("[subagent_tool] shared_context extraction failed: %s", exc)

        # ── Streaming bypass: attach the sub-agent's internal events under this call_subagent tool card ──
        # Only build an emitter when there is an active stream listener (otherwise the
        # uuid/ContextVar reads are wasted). parent_tool_id is taken from the current tool
        # call id (already written into the ContextVar by ActingToolCallIdMiddleware); the
        # frontend uses it to group sub-steps under the right card; when missing, the
        # frontend falls back to grouping under the most recent unmatched call_subagent card.
        from core.llm import _subagent_stream

        # Sub-step counting (best-effort): in the streaming bypass, count one tool call per
        # tool_result received, so the sub-agent call log can show how many tools that call
        # triggered internally. The non-streaming path has no listener; the count stays 0.
        _tool_count = {"n": 0}

        _emit: Optional[Callable[[Dict[str, Any]], None]] = None
        if bool(chat_id) and _subagent_stream.is_active(chat_id):
            try:
                from core.llm.middlewares import CURRENT_TOOL_CALL_ID

                parent_tool_id = CURRENT_TOOL_CALL_ID.get("") or ""
            except Exception:  # noqa: BLE001
                parent_tool_id = ""
            sub_run_id = uuid.uuid4().hex[:12]

            def _emit(sub: Dict[str, Any]) -> None:
                if sub.get("sub_type") == "tool_result":
                    _tool_count["n"] += 1
                _subagent_stream.push(
                    chat_id,
                    {
                        "parent_tool_id": parent_tool_id,
                        "sub_run_id": sub_run_id,
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        **sub,
                    },
                )

            _emit({"sub_type": "start", "task": task[:200]})

        # ── Sub-agent call log: same table as plan_mode (subagent_call_logs), typed
        # user_agent, so "Config console → sub-agent call logs" no longer shows only plan
        # mode — every call_subagent dispatch of a user-built sub-agent becomes an auditable
        # record. Best-effort, never blocks tool execution. ──
        _run_start = time.monotonic()
        _sub_log_id = await log_writer.start_subagent_log(
            {
                "subagent_name": agent_name,
                "subagent_type": "user_agent",
                "subagent_id": agent_id,
                "input_messages": {"task": task, "context_summary": context_summary},
            }
        )

        async def _finish(status: str, *, output: str = "", error: Optional[str] = None) -> None:
            await log_writer.finish_subagent_log(
                _sub_log_id,
                status=status,
                output_content=output or None,
                tool_calls_count=_tool_count["n"],
                error_message=error,
                duration_ms=int((time.monotonic() - _run_start) * 1000),
            )

        try:
            loop = asyncio.get_running_loop()
            # The parent and child must share one governance run. A shallow
            # copy here would let child activations update nested lists while
            # losing scalar changes such as an escalated review_level.
            ontology_runtime = _shared_ontology_runtime(agent_ref)
            ok, text, sub_pinned = await loop.run_in_executor(
                _subagent_pool,
                _run_subagent_in_thread,
                agent_id,
                agent_name,
                task,
                context_summary,
                current_user_id,
                shared_messages,
                _emit,
                ontology_runtime,
            )

            # Feed the files the sub-agent pinned to its workspace back into the main
            # context's workspace: the sub-agent has its own workspace ContextVar in its own
            # thread, while the main conversation's wrap-up (meta artifacts + persistence)
            # reads the main context's pinned list — without the feed-back, deliverable files
            # produced by the sub-agent never show up in the main conversation.
            if sub_pinned:
                try:
                    from core.llm import workspace as _ws

                    for _it in sub_pinned:
                        _ws.pin(
                            file_id=_it.get("file_id"),
                            name=_it.get("name"),
                            mime_type=_it.get("mime_type"),
                            size=_it.get("size"),
                            url=_it.get("url"),
                        )
                    _ws.mark_active()
                except Exception as _exc:  # noqa: BLE001
                    logger.warning("[subagent_tool] re-pin subagent files failed: %s", _exc)

            if _emit is not None:
                _emit({"sub_type": "end", "ok": bool(ok)})

            if not ok:
                await _finish("failed", output=text, error=text)
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=f"子智能体「{agent_name}」执行出错：{text}",
                        )
                    ]
                )

            logger.info(
                "[subagent_tool] call_subagent completed: agent=%s, task_len=%d, response_len=%d",
                agent_name,
                len(task),
                len(text),
            )

            await _finish("success", output=text)
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"【{agent_name}】的回复：\n\n{text}",
                    )
                ]
            )

        except Exception as e:
            logger.error("call_subagent failed: agent=%s, error=%s", agent_id, e, exc_info=True)
            if _emit is not None:
                _emit({"sub_type": "error", "error": str(e)[:200]})
            await _finish("failed", error=str(e)[:200])
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"子智能体「{agent_name}」执行出错：{str(e)[:200]}",
                    )
                ]
            )

    toolkit.register_tool_function(call_subagent, namesake_strategy="skip")


def _get_tools_desc(agent_info: Dict[str, Any]) -> str:
    """Return a short comma-separated list of MCP tools available to the agent."""
    mcp_ids = agent_info.get("mcp_server_ids") or []
    if not mcp_ids:
        return "默认工具集"
    return ", ".join(mcp_ids)


def build_subagent_prompt_section(
    visible_agents: List[Dict[str, Any]],
) -> str:
    """Build the system prompt section describing available sub-agents.

    Per-turn @-mention hints are returned by build_subagent_mention_hint() and
    are kept OUT of the system prompt. Otherwise the system prompt would vary
    every turn (whenever the user @s different agents), defeating the LLM
    provider's prefix cache.
    """
    if not visible_agents:
        return ""

    rows = []
    shared_agents = []
    for a in visible_agents:
        desc = a.get("description", "")
        tools = _get_tools_desc(a)
        has_shared = (a.get("extra_config") or {}).get("shared_context", False)
        ctx_col = "是" if has_shared else "否"
        rows.append(f"| {a['agent_id']} | {a['name']} | {desc} | {tools} | {ctx_col} |")
        if has_shared:
            shared_agents.append(a["name"])

    table = "| ID | 名称 | 适用场景 | 可用工具 | 共享上下文 |\n|---|---|---|---|---|\n" + "\n".join(
        rows
    )

    section = (
        "## 可用子智能体\n\n"
        "你可以通过 `call_subagent` 工具将专业任务分派给子智能体处理。"
        "每个子智能体拥有独立的工具和专业知识。\n\n" + table + "\n\n"
        "### 何时使用子智能体\n"
        "- 任务需要子智能体拥有的专业工具（参见上表「可用工具」列）\n"
        "- 用户通过 @名称 明确指定时\n"
        "- 需要多个独立信息源时，在同一轮并行调用多个子智能体以提高效率\n\n"
        "### 何时不使用子智能体\n"
        "- 你自己的工具已能完成的单步查询或操作\n"
        "- 简单问答或你已有足够信息直接回答的问题\n"
        "- 不确定是否需要时，优先自己处理\n\n"
        "### 编写 task 描述的要求\n"
        "除标注「共享上下文=是」的子智能体外，其余子智能体看不到当前对话历史。"
        "像给一个刚加入的同事布置任务一样编写 task：\n"
        "- 说明要完成什么，以及为什么需要这个信息\n"
        "- 描述你已经了解到或排除了什么\n"
        "- 提供足够的背景让子智能体能做判断，而不是死板执行\n"
        "- 如果需要简短回复，明确说明（如「200字以内」）\n"
        "- **不要委托理解**——不要写「根据你的分析帮我总结」，"
        "而是说明具体要查什么数据、对比什么指标、回答什么问题\n\n"
        "### 共享上下文子智能体\n"
        "标注「共享上下文=是」的子智能体能自动读取当前完整对话历史（含工具调用结果），"
        "无需在 task 中重复传递已有信息。对这类子智能体，task 只需简洁说明要执行的操作。\n\n"
        "### 处理结果\n"
        "- 子智能体的回复对用户不可见，你必须汇总整合后呈现给用户\n"
        "- 多个子智能体的结果需要你做综合分析，不要简单拼接\n"
    )

    return section


def build_subagent_mention_hint(
    visible_agents: List[Dict[str, Any]],
    mentioned_agent_ids: Optional[List[str]] = None,
) -> str:
    """Build a per-turn hint when the user @-mentioned specific subagents.

    Designed to be prepended to the *current user message* so the system prompt
    stays byte-stable across turns (prefix-cache friendly). Returns "" when
    there's nothing to inject.
    """
    if not mentioned_agent_ids or not visible_agents:
        return ""
    agent_map = {a["agent_id"]: a["name"] for a in visible_agents}
    names = [agent_map.get(aid, aid) for aid in mentioned_agent_ids if aid in agent_map]
    if not names:
        return ""
    return (
        f"**用户已指定调用子智能体：{'、'.join(names)}。"
        "请直接使用 call_subagent 工具调用指定的子智能体。**\n"
    )


def build_explicit_subagent_command_hint(
    visible_agents: List[Dict[str, Any]],
    agent_id: str,
) -> str:
    """Constrain an explicit natural-language delegation without bypassing the LLM.

    The hint lives in the current user turn so the stable system prompt remains
    cacheable. The main model still reasons and emits the actual tool call, but
    it cannot reinterpret an unambiguous ``调用 <name> 子智能体`` command as
    permission to query its own tools first.
    """
    target = next(
        (item for item in visible_agents if str(item.get("agent_id") or "") == agent_id),
        None,
    )
    if not target:
        return ""
    agent_name = str(target.get("name") or agent_id)
    return (
        "<explicit_subagent_command>\n"
        f"用户已明确要求调用子智能体「{agent_name}」（agent_id={agent_id}）。\n"
        "你必须保留正常的思考与流式输出，并将下一个工具调用设为 "
        f'call_subagent(agent_id="{agent_id}", task=<下方用户任务>)。\n'
        "调用子智能体之前不得调用其他工具，也不得先自行查询或执行该任务。"
        "子智能体返回后，不再调用其他数据工具，直接基于其结果整合最终回答。\n"
        "</explicit_subagent_command>"
    )

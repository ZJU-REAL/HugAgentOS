"""call_subagent tool — allows the main agent to dispatch tasks to sub-agents.

Each sub-agent runs in an isolated thread with its own event loop to avoid
anyio cancel-scope cross-task errors from MCP clients.
"""

from __future__ import annotations
from core.llm.factory.tools.definitions import function_tool
from core.harness import log_writer

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolChunk).
from agentscope.tool._response import ToolChunk
from core.infra.task_lifecycle import settle_task
from core.llm.subagents import sessions as subagent_sessions


from core.llm.subagents.execution import _ChildExecution, _map_subagent_event, _run_subagent_in_thread, _shared_ontology_runtime, _SubMapper
from core.llm.subagents.prompts import _get_tools_desc as get_tools_desc, build_explicit_subagent_command_hint, build_subagent_mention_hint, build_subagent_prompt_section

logger = logging.getLogger(__name__)

_subagent_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subagent")


def _child_capability_runtime(parent_runtime, agent_id):
    runtime = dict(parent_runtime or {})
    from core.harness.registry.run_capabilities import has_run_capabilities

    if runtime.get("run_id") and has_run_capabilities():
        from core.harness.registry.capability_runtime import child_scope
        from core.harness.tool_call_context import CURRENT_TOOL_CALL_ID

        # The ledger's persisted tool-call identity is stable on replay. SSE's
        # random sub_run_id is presentation-only and never defines authority.
        runtime["capability_scope"] = child_scope(
            str(runtime.get("capability_scope") or ""),
            "subagent",
            CURRENT_TOOL_CALL_ID.get(""),
            agent_id,
        )
    return runtime


def register_subagent_tool(
    toolkit: Toolkit,
    visible_agents: List[Dict[str, Any]],
    current_user_id: str,
    agent_ref: Optional[Dict] = None,
    chat_id: Optional[str] = None,
    parent_runtime: Optional[Dict[str, Any]] = None,
) -> None:
    """Register the call_subagent tool into the main agent's toolkit.

    Args:
        agent_ref: Mutable container ``{"agent": None}`` — set to the main
            agent instance after creation.  Used to extract shared context
            for sub-agents that have ``extra_config.shared_context == True``.
        chat_id: This run's chat_id — sub-agent streaming events are bypass-routed back
            to the main SSE stream by it (see core/llm/subagents/stream.py).
    """
    agent_map = {a["agent_id"]: a for a in visible_agents}

    async def call_subagent(
        agent_id: str,
        task: str,
        context_summary: str = "",
        resume_session_id: str = "",
    ) -> ToolChunk:
        """调用子智能体执行专业任务。子智能体拥有独立的工具和专业知识。

        需要并行调用多个子智能体时，在同一轮回复中生成多个 call_subagent 调用，
        系统会自动并行执行。分派判据、task 写法、并行约束与续跑规则见系统提示的
        「可用子智能体」一节。

        Args:
            agent_id (`str`):
                要调用的子智能体 ID（参见系统提示中的可用子智能体列表）。
            task (`str`):
                完整的任务描述：要完成什么及为什么、已知的背景信息、需要回答的具体问题。
            context_summary (`str`):
                当前对话的关键背景摘要（可选）。应包含与任务相关的已知事实，
                而非完整对话记录。
            resume_session_id (`str`):
                续跑句柄（可选）。填入上一次调用返回的句柄，该子智能体会在上一轮的
                完整上下文（读过的文件、跑过的命令、犯过的错）上继续；留空则全新启动。

        Returns:
            `ToolChunk`:
                子智能体的执行结果，末尾附带本次的续跑句柄。
                结果对用户不可见，你需要汇总后呈现给用户。
        """
        if agent_id not in agent_map:
            return ToolChunk(
                content=[
                    TextBlock(
                        type="text",
                        text=f"错误：子智能体 {agent_id} 不存在或无权访问。请检查 ID 是否正确。",
                    )
                ]
            )

        agent_info = agent_map[agent_id]
        agent_name = agent_info.get("name", agent_id)

        # Resume: continue the same child on the context it built last time.
        resume_handle = (resume_session_id or "").strip()
        resume_messages: Optional[List[Dict[str, Any]]] = None
        if resume_handle:
            resume_messages = await subagent_sessions.load(
                handle=resume_handle,
                agent_id=agent_id,
                user_id=current_user_id,
            )
            if resume_messages is None:
                return ToolChunk(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"续跑句柄 {resume_handle} 已失效或不属于子智能体「{agent_name}」。"
                                "请去掉 resume_session_id 重新分派，并在 task 里补上必要背景。"
                            ),
                        )
                    ]
                )

        # Check whether shared context is enabled. A resumed run already carries
        # whatever it inherited the first time, so it is not re-inherited here.
        shared_context = (agent_info.get("extra_config") or {}).get("shared_context", False)
        shared_messages = None
        if not resume_messages and shared_context and agent_ref and agent_ref.get("agent"):
            try:
                from core.llm.runtime.message_compat import extract_messages_from_context

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
        from core.llm.subagents import stream as _subagent_stream

        # Sub-step counting (best-effort): in the streaming bypass, count one tool call per
        # tool_result received, so the sub-agent call log can show how many tools that call
        # triggered internally. The non-streaming path has no listener; the count stays 0.
        _tool_count = {"n": 0}

        _emit: Optional[Callable[[Dict[str, Any]], None]] = None
        if bool(chat_id) and _subagent_stream.is_active(chat_id):
            try:
                from core.harness.tool_call_context import CURRENT_TOOL_CALL_ID

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
        # user_agent/builtin_<role>, so every call_subagent dispatch becomes an
        # auditable record. Best-effort, never blocks tool execution. ──
        builtin_role = (agent_info.get("extra_config") or {}).get("builtin_role")
        _run_start = time.monotonic()
        _sub_log_id = uuid.uuid4().hex
        _log_start = asyncio.create_task(
            log_writer.start_subagent_log(
                {
                    "id": _sub_log_id,
                    "subagent_name": agent_name,
                    "subagent_type": f"builtin_{builtin_role}" if builtin_role else "user_agent",
                    "subagent_id": agent_id,
                    "input_messages": {
                        "task": task,
                        "context_summary": context_summary,
                        "resume_session_id": resume_handle,
                    },
                }
            )
        )
        _execution = _ChildExecution()
        _worker = None
        _submitted = None
        _terminal = None

        async def _finish(status: str, *, output: str = "", error: Optional[str] = None) -> None:
            nonlocal _terminal

            async def record_terminal():
                await _log_start
                await log_writer.finish_subagent_log(
                    _sub_log_id,
                    status=status,
                    output_content=output or None,
                    tool_calls_count=_tool_count["n"],
                    error_message=error,
                    duration_ms=int((time.monotonic() - _run_start) * 1000),
                )
                if _emit is not None:
                    _emit({"sub_type": "end", "ok": status == "success", "status": status})

            if _terminal is None:
                _terminal = asyncio.create_task(record_terminal())
            await asyncio.shield(_terminal)

        try:
            await asyncio.shield(_log_start)
            loop = asyncio.get_running_loop()
            # The parent and child must share one governance run. A shallow
            # copy here would let child activations update nested lists while
            # losing scalar changes such as an escalated review_level.
            ontology_runtime = _shared_ontology_runtime(agent_ref)
            from core.harness.capability_errors import IntegrityFailed

            try:
                child_runtime = _child_capability_runtime(parent_runtime, agent_id)
            except IntegrityFailed:
                await _finish("failed", error="missing durable capability scope")
                return ToolChunk(
                    content=[
                        TextBlock(
                            type="text", text="子智能体缺少可恢复的持久工具调用身份，已拒绝执行。"
                        )
                    ],
                    state="error",
                )
            _submitted = _subagent_pool.submit(
                _run_subagent_in_thread,
                agent_id,
                agent_name,
                task,
                context_summary,
                current_user_id,
                shared_messages,
                _emit,
                ontology_runtime,
                child_runtime,
                resume_messages,
                _execution,
            )
            _worker = asyncio.wrap_future(_submitted, loop=loop)
            ok, text, sub_pinned, final_messages = await asyncio.shield(_worker)

            # Feed the files the sub-agent pinned to its workspace back into the main
            # context's workspace: the sub-agent has its own workspace ContextVar in its own
            # thread, while the main conversation's wrap-up (meta artifacts + persistence)
            # reads the main context's pinned list — without the feed-back, deliverable files
            # produced by the sub-agent never show up in the main conversation.
            if sub_pinned:
                try:
                    from core.llm.runtime import workspace as _ws

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

            if not ok:
                await _finish("failed", output=text, error=text)
                return ToolChunk(
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

            # Hand the parent a handle onto this child's context so a follow-up
            # task can continue it instead of re-deriving what it already knows.
            next_handle = resume_handle or subagent_sessions.new_handle()
            saved = await subagent_sessions.save(
                handle=next_handle,
                agent_id=agent_id,
                user_id=current_user_id,
                chat_id=chat_id,
                messages=final_messages,
            )
            await _finish("success", output=text)
            resume_note = (
                f"\n\n（续跑句柄：{next_handle}。需要该子智能体在此基础上继续时，"
                "把 resume_session_id 设为它。）"
                if saved
                else ""
            )
            return ToolChunk(
                content=[
                    TextBlock(
                        type="text",
                        text=f"【{agent_name}】的回复：\n\n{text}{resume_note}",
                    )
                ]
            )

        except asyncio.CancelledError:
            _execution.cancel()
            if _submitted is not None:
                _submitted.cancel()  # removes queued work; running work uses the task bridge

            async def finish_cancelled():
                # Wait for the actual worker, not the cancelled asyncio wrapper.
                # A queued child binds the already-cancelled execution before
                # its first await, so it cannot start a model/tool call.
                if _worker is not None:
                    try:
                        await settle_task(_worker)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("subagent cleanup failed after cancellation")
                await settle_task(_log_start)
                await _finish("cancelled", error="parent task cancelled")

            await settle_task(asyncio.create_task(finish_cancelled()))
            raise
        except Exception as e:
            logger.error("call_subagent failed: agent=%s, error=%s", agent_id, e, exc_info=True)
            if _emit is not None:
                _emit({"sub_type": "error", "error": str(e)[:200]})
            await _finish("failed", error=str(e)[:200])
            return ToolChunk(
                content=[
                    TextBlock(
                        type="text",
                        text=f"子智能体「{agent_name}」执行出错：{str(e)[:200]}",
                    )
                ]
            )

    toolkit.append(function_tool(call_subagent))

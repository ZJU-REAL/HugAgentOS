"""Batch execution orchestrator (Phase 2 of the batch flow).

Once the user has confirmed a BatchPlan via the UI dialog, this module
serially iterates over plan.items, spawns a fresh ReActAgent per item
(with batch_runner disabled to prevent recursion), and persists per-item
results back into the plan so the SSE follower can replay them on
reconnect.

Architecture (refresh-survival):

    SSE handler  ──follow──▶  background asyncio.Task   ──writes──▶  DB
        │                       (one per plan_id)                     ▲
        │                              │                              │
        └──── reads stored results + tails new ones ──────────────────┘

The background task is created on the first /stream call for a plan and
keeps running even if the SSE client disconnects (page refresh, tab
switch). When the client reconnects we look up the same task — if it's
still alive we tail; if it finished we replay results from the DB.

Failures are retried up to plan.max_retries with exponential back-off;
exhausted retries are recorded as ``status="skipped"`` and the loop
continues. Setting plan.status='cancelled' in DB stops the loop at the
next iteration boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from core.config.catalog import get_enabled_ids
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.config.catalog_resolver import resolve_all_runtime_enabled
from core.db.engine import SessionLocal
from core.db.models import BatchPlan
from core.llm.agent_factory import create_agent_executor
from core.llm.message_compat import strip_thinking

logger = logging.getLogger(__name__)


# Plan status values — matches the CHECK constraint in
# alembic v2w3x4y5z6a7_add_batch_plans.
PENDING = "pending"
CONFIRMED = "confirmed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = {DONE, CANCELLED, FAILED}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_format(template: str, item: Dict[str, Any]) -> str:
    """Render template via straight ``{key}`` substitution.

    We deliberately avoid ``str.format_map`` here because Python format
    syntax treats ``:`` as a format-spec separator and ``!`` as a
    conversion flag — both are common in real-world column names
    (e.g. "营收:亿元", "比率(%)"). A simple replace also makes missing
    keys harmless: they keep their literal placeholder.
    """
    if not isinstance(item, dict) or not template:
        return template or ""
    out = template
    for k, v in item.items():
        if not isinstance(k, str):
            continue
        placeholder = "{" + k + "}"
        if placeholder in out:
            out = out.replace(placeholder, "" if v is None else str(v))
    return out


def _summarize_item(item: Dict[str, Any], source_type: str, max_len: int = 80) -> str:
    """One-line preview of an item, used in ``batch_item_start`` events."""
    if not isinstance(item, dict):
        text = str(item)
    elif source_type == "text_list":
        text = str(item.get("text") or item.get("content") or "")
    elif source_type == "word_files":
        text = str(item.get("file_name") or item.get("title") or "")
    elif source_type == "xlsx":
        parts = []
        for k, v in item.items():
            if k in ("row", "row_index"):
                continue
            parts.append(f"{k}={v}")
            if len(parts) >= 2:
                break
        text = " | ".join(parts) or f"row {item.get('row', '?')}"
    else:
        text = str(item)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_plan(db, plan_id: str) -> Optional[BatchPlan]:
    return db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()


def _get_status(plan_id: str) -> Optional[str]:
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        return plan.status if plan else None


def _set_status(plan_id: str, status: str) -> None:
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if plan:
            plan.status = status
            plan.updated_at = datetime.utcnow()
            db.commit()


def _read_results(plan_id: str) -> tuple[List[Dict[str, Any]], str, Dict[str, int]]:
    """Return (item_results, status, progress_counts)."""
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if not plan:
            return [], "missing", {"done": 0, "success": 0, "failed": 0}
        progress = dict(plan.progress or {})
        results = list(progress.get("results") or [])
        counts = {
            "done": int(progress.get("done", 0)),
            "success": int(progress.get("success", 0)),
            "failed": int(progress.get("failed", 0)),
        }
        return results, plan.status, counts


def _append_result(plan_id: str, result: Dict[str, Any]) -> Dict[str, int]:
    """Atomically append a single item result to plan.progress.results
    and bump the success/failed/done counters. Returns updated counts."""
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if not plan:
            return {"done": 0, "success": 0, "failed": 0}
        prog = dict(plan.progress or {})
        results: List[Dict[str, Any]] = list(prog.get("results") or [])
        results.append(result)
        prog["results"] = results
        prog["done"] = int(prog.get("done", 0)) + 1
        if result.get("status") == "success":
            prog["success"] = int(prog.get("success", 0)) + 1
        else:
            prog["failed"] = int(prog.get("failed", 0)) + 1
        plan.progress = prog
        # SQLAlchemy needs a hint that the JSONB column was mutated.
        flag_modified(plan, "progress")
        plan.updated_at = datetime.utcnow()
        db.commit()
        return {
            "done": prog["done"],
            "success": prog["success"],
            "failed": prog["failed"],
        }


# ---------------------------------------------------------------------------
# Background task registry
# ---------------------------------------------------------------------------


_active_tasks: Dict[str, asyncio.Task] = {}
_registry_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the lock so this module is importable outside an event loop."""
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def _ensure_background_task(plan_id: str, user_id: str) -> None:
    """Start the background runner for *plan_id* if it isn't already running.

    The task survives client disconnect — it iterates plan.items, persists
    per-item results to DB, and exits when all items are processed (or the
    plan is cancelled). Subsequent /stream reconnects find the live task
    here and tail its output via DB polling.
    """
    async with _get_lock():
        existing = _active_tasks.get(plan_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(
            _run_until_done(plan_id, user_id),
            name=f"batch_runner:{plan_id}",
        )
        _active_tasks[plan_id] = task

        def _cleanup(t: asyncio.Task) -> None:
            # Remove from registry once finished. Log unexpected errors.
            _active_tasks.pop(plan_id, None)
            if t.cancelled():
                logger.info("[batch] runner task cancelled plan_id=%s", plan_id)
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "[batch] runner task crashed plan_id=%s: %s", plan_id, exc,
                )

        task.add_done_callback(_cleanup)


def cancel_running_task(plan_id: str) -> bool:
    """Cancel the in-flight runner task for *plan_id* if one exists.

    Without this, /cancel only flags the DB and the runner keeps blocking
    on the current item's LLM call — the user sees no response until that
    item completes (often 30s+). Cancelling the asyncio task interrupts
    the current item immediately.

    The task's done_callback removes it from the registry; ``_run_until_done``
    won't write a final status because cancellation propagates as
    CancelledError, but that's fine — the /cancel endpoint already set
    plan.status = CANCELLED in the DB.

    Returns True if a task was found and cancellation was requested.
    """
    task = _active_tasks.get(plan_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ---------------------------------------------------------------------------
# Background runner — runs detached from the SSE handler
# ---------------------------------------------------------------------------


async def _fallback_recover_final_text(agent: Any, prompt_preview: str) -> str:
    """Fallback recovery when the main path failed to capture a final reply.

    Two steps:
      Step 1 — Walk memory in reverse, accepting **only assistant messages without tool_use blocks**.
               Those are the genuine text-only terminal replies; this never mistakes a prelude
               like "Now calling tool X:" for the result.
      Step 2 — If Step 1 found nothing, the agent exited abnormally before synthesizing a final
               reply (compaction failure / upstream returned empty / gateway timeout, etc.).
               Inject a system-hint into memory and call ``agent.reply()`` once more to force the
               model to produce a final Chinese answer based on the existing tool_results. If that
               also fails, return an empty string so the caller records ``status="skipped"``
               instead of treating a dirty prelude as success.
    """
    from agentscope.message import Msg, TextBlock
    from core.llm.message_compat import extract_text_from_chat_response

    # ── Step 1 ──
    try:
        # AgentScope 2.0: agent.memory removed → agent.state.context: list[Msg]
        mem = list(agent.state.context)
        for msg in reversed(mem or []):
            if getattr(msg, "role", None) != "assistant":
                continue
            has_tu = (
                msg.has_content_blocks("tool_call")  # 2.0: tool_use → tool_call
                if hasattr(msg, "has_content_blocks")
                else False
            )
            if has_tu:
                # Skip — this one is just a reasoning prelude to a tool call, not the terminal reply.
                continue
            text = (extract_text_from_chat_response(msg) or "").strip()
            if text:
                logger.info(
                    "[batch] fallback step1: picked text-only assistant len=%d "
                    "preview=%r prompt=%r",
                    len(text), text[:120], prompt_preview,
                )
                return text
    except Exception as e:
        logger.warning("[batch] fallback step1 memory walk failed: %s", e)

    # ── Step 2 ──
    logger.warning(
        "[batch] fallback step1 found nothing — entering step2: re-prompt "
        "synthesis. prompt=%r", prompt_preview,
    )
    try:
        synthesis_hint = (
            "<system-hint>之前的工具调用已完成，但你尚未给出最终答复。"
            "请基于已获取的工具结果，**直接用中文写出最终答案**，"
            "不要再调用任何工具，不要再输出推理过程或新的工具调用计划，"
            "只输出给用户看的正文。</system-hint>"
        )
        agent.state.context.append(Msg(
            name="user", role="user",
            content=[TextBlock(type="text", text=synthesis_hint)],
        ))
        reply_msg = await agent.reply(inputs=None)
        text = (extract_text_from_chat_response(reply_msg) or "").strip()
        if text:
            logger.info(
                "[batch] fallback step2: synthesis succeeded len=%d preview=%r",
                len(text), text[:120],
            )
            return text
        logger.warning("[batch] fallback step2: synthesis returned empty text")
    except Exception as e:
        logger.warning("[batch] fallback step2 synthesis failed: %s", e)

    # Everything failed: return empty so the caller classifies it as skipped/incomplete.
    return ""


async def _run_item_via_workflow(
    prompt: str,
    user_id: str,
    sub_mcp_ids: List[str],
    *,
    sub_skill_ids: Optional[List[str]] = None,
    sub_visible_agents: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run a single batch item through the streaming-agent pipeline.

    Captures the same surface the regular chat bubble renders:
      • `text` — concatenated text deltas (final answer)
      • `tool_calls` — per call: name, display_name, args, output, status
      • `artifacts` — file references extracted from tool results
      • `citations` — knowledge-base / search citations

    Returns ``(text, tool_calls, artifacts, citations)``.
    """
    from core.content.artifact_refs import extract_file_refs
    from core.chat.tool_log import (
        attach_tool_result as _attach_tool_result,
        upsert_tool_call as _upsert_tool_call,
    )
    from core.services.artifact_service import extend_collected_artifacts as _extend_collected_artifacts
    from core.config.display_names import TOOL_DISPLAY_NAMES
    from core.llm import workspace as _workspace_mod
    from core.llm.message_compat import extract_text_from_chat_response
    from orchestration.citations import extract_citations_with_offset
    from orchestration.streaming import StreamingAgent

    tool_calls_log: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, Any]] = []
    citations: List[Dict[str, Any]] = []
    # When the same tool is called multiple times within one item, citation ids restart
    # from 1 (see the ``<tool_name>-<index>`` naming in routing/citations.py). Without an
    # offset, ``internet_search-1`` would appear repeatedly in the citations list, and
    # downstream id-based lookup/dedup would mis-locate or drop entries. The main chat path
    # (routing/workflow.py) uses the same extract_citations_with_offset helper; shared here.
    citation_offsets: Dict[str, int] = {}
    from core.services.ontology_service import build_user_ontology_runtime

    ontology_enabled, ontology_runtime = build_user_ontology_runtime(
        user_id=user_id,
        task=prompt,
    )

    # ⚠️ isolated=True is mandatory for the batch runner. Two things must both happen:
    #   1) skip the shared MCP pool and spawn brand-new stdio subprocesses per item
    #   2) skip HTTP-transport MCP (streamable_http / sse)
    #
    # Going back to isolated=False and reusing the pool was tried: StreamingAgent has a
    # _wait_reply background task that force-cancels reply_task 10s after the stream ends,
    # and that cancel, exiting through the pool's shared anyio scope, triggers a cross-task
    # bug which cancels the next item's setup as collateral — symptom: item 0 OK, but item 1
    # gets cancelled right at startup. In other words, as long as anyio resources are shared
    # across items, the cancel-scope cross-task landmine remains.
    #
    # Every item must get its own stdio clients (no cross-contamination) + no HTTP calls
    # (when an HTTP client is GC'd, its __aexit__ exiting the anyio scope in the wrong task
    # is the same underlying bug; see history commits).
    agent, clients = await create_agent_executor(
        user_query=prompt,
        enabled_skill_ids=sub_skill_ids,
        enabled_mcp_ids=sub_mcp_ids,
        current_user_id=user_id,
        isolated=True,
        max_iters=50,
        visible_subagents=sub_visible_agents if sub_visible_agents else None,
        # Batch items run concurrently: force an ephemeral sandbox — each item gets its own
        # temporary container, reclaimed on finish — so multiple items don't fight over the
        # same persistent kernel state and cross-contaminate (docs §4.1).
        sandbox_session_id="",
        ontology_runtime=ontology_runtime,
    )
    streaming_agent = StreamingAgent(agent, clients)

    # In a ReAct loop the agent emits multiple rounds of text deltas:
    # round 1 reasoning → tool_call → tool_result → round 2 reasoning →
    # tool_call → ... → final answer. We only want the LAST round
    # (the final answer), so we reset ``current_round_text`` whenever a
    # new tool_call fires — anything still buffered at end of stream is
    # the final answer.
    current_round_text = ""
    prompt_preview = (prompt or "")[:80]
    # Strict workspace gate: each batch item gets its own scoped state so
    # nothing leaks between items or back to the parent caller.
    _ws_ctx = _workspace_mod.scope()
    _ws_ctx.__enter__()
    try:
        # NOTE: do NOT pre-add the prompt to agent.memory here.
        # ``StreamingAgent.stream()`` pops the last user message out of
        # ``session_messages`` and passes it to ``agent.reply(user_msg)``,
        # which adds it to memory internally. Manually adding it first
        # causes the same prompt to appear **twice** in agent memory,
        # which (a) wastes context budget, (b) makes the OpenAI formatter
        # emit two identical user-role messages in a row, confusing the
        # model and (c) breaks any pre_reply hook that walks "the last
        # user message" — it finds the manual copy instead of the one
        # ``reply()`` is about to add. The main chat path in
        # ``workflow.py`` already follows this contract (see comment at
        # workflow.py:263 — "agent.reply() will add it to memory
        # internally, avoiding duplicates").
        async for event_type, payload in streaming_agent.stream(
            session_messages=[{"role": "user", "content": prompt}],
            context={
                "user_id": user_id,
                "model_name": DEFAULT_CHAT_MODEL_ALIAS,
                "enable_thinking": False,
                "ontology_enabled": ontology_enabled,
                "ontology_runtime": ontology_runtime,
            },
        ):
            if event_type == "text_delta":
                current_round_text += payload
            elif event_type == "tool_call":
                # Reasoning-prelude text was a setup for *this* tool call;
                # it isn't the final answer to the user, so drop it.
                current_round_text = ""

                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                tool_id = payload.get("id", "")
                _upsert_tool_call(tool_calls_log, {
                    "tool_name": tool_name,
                    "tool_display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
                    "tool_args": tool_args if isinstance(tool_args, dict) else {},
                    "tool_id": tool_id,
                })
            elif event_type == "tool_result":
                tool_name = payload.get("name", "unknown")
                tool_id = payload.get("id", "")
                tool_content = payload.get("content", "")
                try:
                    tool_result_json = json.loads(tool_content) if tool_content else {}
                except (json.JSONDecodeError, TypeError):
                    tool_result_json = {"result": tool_content}
                _attach_tool_result(tool_calls_log, tool_id, tool_name, tool_result_json)
                # Pull file/artifact refs out of the tool result so the UI
                # can render Word/Excel/PPT/chart download cards.
                refs = extract_file_refs(tool_result_json)
                for ref in refs:
                    ref["tool_name"] = tool_name or ""
                _extend_collected_artifacts(artifacts, refs)
                # Citations (KB hits, internet search). The offset logic shares
                # extract_citations_with_offset with the main chat path, avoiding
                # id collisions when the same tool is called multiple times.
                cit_items = extract_citations_with_offset(
                    tool_name, tool_id, tool_result_json, citation_offsets
                )
                citations.extend([c.to_dict() for c in cit_items])
            elif event_type == "error":
                if isinstance(payload, BaseException):
                    raise payload
                raise RuntimeError(str(payload))
            # heartbeat / tool_pending / thinking_delta — ignored for batch

        full_response = current_round_text

        # Fallback: main path empty → walk memory in reverse for a text-only assistant
        # message; if none, inject a system-hint and have the model re-synthesize.
        # See _fallback_recover_final_text for details.
        if not full_response.strip():
            full_response = await _fallback_recover_final_text(
                agent=agent,
                prompt_preview=prompt_preview,
            )

        from core.ontology.validator import requires_output_review

        if full_response and requires_output_review(ontology_runtime):
            from orchestration.subagents.ontology_reviewer import review_ontology_output

            trace = [
                {
                    "type": "tool_result",
                    "tool_name": item.get("tool_name"),
                    "tool_id": item.get("tool_id"),
                    "result": item.get("result"),
                }
                for item in tool_calls_log
                if "result" in item
            ]
            review = await review_ontology_output(
                task=prompt,
                answer=full_response,
                runtime=ontology_runtime,
                trace=trace,
                citations=citations,
                user_id=user_id,
                chat_id=None,
                model_name=DEFAULT_CHAT_MODEL_ALIAS,
            )
            full_response = review["answer"]

        # Strict workspace gate: replace artifacts list with the pinned
        # subset BEFORE leaving the scope (state is reset on __exit__).
        artifacts = _workspace_mod.get_pinned()
    finally:
        try:
            _ws_ctx.__exit__(None, None, None)
        except Exception:
            pass
        # Deliberately no cleanup: (streaming_agent, clients) are all stuffed into a
        # process-level resident list so they are never GC'd during the batch. Once GC runs,
        # the AsyncExitStack.__aexit__ of the clients exits the anyio cancel scope from the
        # wrong task, and the cancel signal propagates along the event loop to the next item
        # → that item gets cancelled on the spot. The cost is that subprocesses + fds/sockets
        # linger until the worker process exits — acceptable compared to a hang.
        _persistent_clients.append((streaming_agent, list(clients)))

    return full_response, tool_calls_log, artifacts, citations


# Per-process strong-ref list — every (streaming_agent, clients) tuple
# created during a batch run gets appended here and is never freed. The
# point is to keep Python from GC-ing them mid-batch: GC would call
# AsyncExitStack.__aexit__ on the MCP clients in some background task,
# which exits anyio cancel scopes from the wrong task and that cancel
# signal cascades into whichever item is currently spawning subprocesses.
# Released only when the worker process exits / is restarted.
_persistent_clients: list = []


async def _run_until_done(plan_id: str, user_id: str) -> None:
    """Process unfinished items one by one, persisting results to DB.

    Idempotent: if the task is restarted (e.g. process restart), it
    resumes from len(progress.results) since already-stored entries are
    skipped on the next iteration.
    """
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if not plan:
            logger.warning("[batch] plan %s not found, runner aborting", plan_id)
            return
        if plan.user_id != user_id:
            logger.warning("[batch] plan %s owner mismatch, runner aborting", plan_id)
            return
        items: List[Dict[str, Any]] = list(plan.items or [])
        template: str = plan.prompt_template or ""
        max_retries: int = int(plan.max_retries or 2)
        source_type: str = plan.source_type
        # Mark running on first launch, idempotent on resume.
        if plan.status not in _TERMINAL:
            plan.status = RUNNING
            plan.updated_at = datetime.utcnow()
            db.commit()

    # Resolve the same capability set the user has configured in the
    # regular chat catalog (skills + MCP tools + sub-agents), so the
    # batch sub-agent sees exactly what they see in normal chat.
    # Fallback to global catalog defaults if resolution fails so a DB
    # hiccup doesn't strand the runner with zero tools.
    sub_skill_ids: Optional[List[str]] = None
    sub_mcp_ids: List[str]
    sub_visible_agents: List[Dict[str, Any]] = []
    with SessionLocal() as _cap_db:
        try:
            _skills, _agents, _mcps = resolve_all_runtime_enabled(_cap_db, user_id)
        except Exception as _exc:  # pragma: no cover - defensive
            logger.warning("[batch] capability resolve failed plan=%s: %s", plan_id, _exc)
            _skills, _agents, _mcps = None, None, None
        # MCPs: prefer user-effective list; fall back to global; always drop
        # batch_runner so the sub-agent can't recurse into another batch.
        if _mcps is None:
            sub_mcp_ids = [m for m in get_enabled_ids("mcp") if m != "batch_runner"]
        else:
            sub_mcp_ids = [m for m in _mcps if m != "batch_runner"]
        # Skills: pass through (None = factory default).
        sub_skill_ids = _skills

        # User-defined sub-agents (the @-mentionable kind). Mirrors the
        # routing/workflow.py path — list all the user's sub-agents so
        # the batch agent can call them via call_subagent, same as in a
        # regular chat. Filter against the catalog `agents` allowlist
        # if one is configured so disabled built-ins stay disabled.
        try:
            from core.services.user_agent_service import UserAgentService
            sub_visible_agents = UserAgentService(_cap_db).list_for_user(user_id) or []
        except Exception as _exc:  # pragma: no cover - defensive
            logger.warning("[batch] visible_subagents load failed plan=%s: %s", plan_id, _exc)
            sub_visible_agents = []

    while True:
        # Re-read plan state at the top of every iteration so cancellation
        # and concurrent edits propagate quickly.
        results, status, _ = _read_results(plan_id)
        if status == CANCELLED:
            return
        next_idx = len(results)
        if next_idx >= len(items):
            break

        item = items[next_idx]
        item_summary = _summarize_item(item, source_type)
        logger.info(
            "[batch] plan=%s starting item %d/%d (%s)",
            plan_id, next_idx, len(items), item_summary,
        )

        success_text: Optional[str] = None
        last_error: Optional[str] = None
        attempt_count = 0

        item_tool_calls: List[Dict[str, Any]] = []
        item_artifacts: List[Dict[str, Any]] = []
        item_citations: List[Dict[str, Any]] = []

        for attempt in range(max_retries + 1):
            attempt_count = attempt
            # Reset captured side-channels for each retry so a partial
            # failure doesn't leak its half-done tool calls into the
            # final record.
            item_tool_calls = []
            item_artifacts = []
            item_citations = []
            try:
                prompt = _safe_format(
                    template, item if isinstance(item, dict) else {"item": item}
                )
                accumulated_text, item_tool_calls, item_artifacts, item_citations = \
                    await _run_item_via_workflow(
                        prompt,
                        user_id,
                        sub_mcp_ids,
                        sub_skill_ids=sub_skill_ids,
                        sub_visible_agents=sub_visible_agents,
                    )
                success_text = strip_thinking(accumulated_text).strip()
                break
            except asyncio.CancelledError:
                # The user cancelled (/cancel) or upstream really cancelled us —
                # let CancelledError bubble all the way up to _run_until_done, so the
                # task enters the cancelled state and done_callback records the reason.
                # Never translate the cancel into "skipped" here, or the remaining
                # items would keep running after a user cancellation.
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "[batch] plan=%s item %d attempt %d failed: %s",
                    plan_id, next_idx, attempt, last_error,
                )
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 8))

        record: Dict[str, Any]
        if success_text is not None:
            record = {
                "index": next_idx,
                "status": "success",
                "content": success_text,
                "retry_count": attempt_count,
                "item_summary": item_summary,
                # Captured during the sub-agent run via StreamingAgent — gives
                # the frontend the same building blocks (tool calls, artifacts,
                # citations) that the regular chat bubble uses, so any output
                # format the agent produces (Word/Excel/PPT exports, charts,
                # KB citations) renders without bespoke UI.
                "tool_calls": item_tool_calls,
                "artifacts": item_artifacts,
                "citations": item_citations,
            }
        else:
            record = {
                "index": next_idx,
                "status": "skipped",
                "error": last_error or "unknown error",
                "retry_count": attempt_count,
                "item_summary": item_summary,
                "tool_calls": item_tool_calls,
                "artifacts": item_artifacts,
                "citations": item_citations,
            }
        counts_after = _append_result(plan_id, record)
        logger.info(
            "[batch] plan=%s item %d/%d persisted status=%s done=%d/%d",
            plan_id, next_idx, len(items), record.get("status"),
            counts_after.get("done", 0), len(items),
        )

    # All items processed (or items list was empty). Mark done unless
    # we were cancelled in the meantime.
    final_status = CANCELLED if _get_status(plan_id) == CANCELLED else DONE
    _set_status(plan_id, final_status)
    logger.info("[batch] plan=%s runner finished status=%s", plan_id, final_status)


# ---------------------------------------------------------------------------
# SSE follower — survives reconnects by reading DB state
# ---------------------------------------------------------------------------


_POLL_INTERVAL_SEC = 0.4
# After this much silence, emit a batch_heartbeat event as keepalive, so nginx /
# intermediate reverse proxies / client proxies don't drop the idle connection
# during long LLM calls.
# The frontend batchStore ignores unknown event types, so no extra handling is needed.
_HEARTBEAT_INTERVAL_SEC = 15.0


class BatchOrchestrator:
    """Yield SSE-shaped events for a plan's execution.

    On every /stream call this:
      1. Verifies plan + ownership
      2. Ensures the background runner task is alive
      3. Emits known item_start/item_done pairs from plan.progress.results
      4. Polls DB for new results until the plan is done or cancelled
      5. Emits final batch_done

    Because reading is decoupled from the producer, refreshing the page or
    switching chats does NOT stop execution — it only disconnects the
    follower. Reconnecting replays everything.
    """

    def __init__(self, plan_id: str, user_id: str):
        self.plan_id = plan_id
        self.user_id = user_id

    async def run(self) -> AsyncIterator[Dict[str, Any]]:
        # ── Verify access + state ───────────────────────────────────────
        with SessionLocal() as db:
            plan = _load_plan(db, self.plan_id)
            if plan is None:
                yield {"type": "batch_error", "plan_id": self.plan_id,
                       "error": "plan not found"}
                return
            if plan.user_id != self.user_id:
                yield {"type": "batch_error", "plan_id": self.plan_id,
                       "error": "permission denied"}
                return
            if plan.status not in (CONFIRMED, RUNNING, DONE):
                yield {"type": "batch_error", "plan_id": self.plan_id,
                       "error": f"plan not runnable (status={plan.status})"}
                return
            total = len(plan.items or [])
            source_type = plan.source_type
            items_summary = [
                _summarize_item(it, source_type) for it in (plan.items or [])
            ]

        # ── Kick off (or attach to) the background runner ──────────────
        # Skip the kick-off if the plan is already terminal so we just
        # replay history.
        if _get_status(self.plan_id) not in _TERMINAL:
            await _ensure_background_task(self.plan_id, self.user_id)

        # ── Replay + tail phase ────────────────────────────────────────
        emitted = 0
        last_yield_ts = asyncio.get_event_loop().time()
        while True:
            results, status, counts = _read_results(self.plan_id)

            # Emit any new (or initial-replay) results.
            while emitted < len(results):
                rec = results[emitted]
                idx = rec.get("index", emitted)
                summary = rec.get("item_summary") or (
                    items_summary[idx] if idx < len(items_summary) else f"item {idx}"
                )
                yield {
                    "type": "batch_item_start",
                    "plan_id": self.plan_id,
                    "index": idx,
                    "total": total,
                    "item_summary": summary,
                }
                done_event = {
                    "type": "batch_item_done",
                    "plan_id": self.plan_id,
                    "index": idx,
                    "total": total,
                    "status": rec.get("status", "success"),
                    "retry_count": rec.get("retry_count", 0),
                    "progress": counts,
                    # Pass through everything the chat bubble can render so
                    # the frontend uses its existing primitives (markdown,
                    # tool cards, file/artifact cards, citation chips).
                    "tool_calls": rec.get("tool_calls") or [],
                    "artifacts": rec.get("artifacts") or [],
                    "citations": rec.get("citations") or [],
                }
                if rec.get("status") == "success":
                    done_event["content"] = rec.get("content", "")
                else:
                    done_event["error"] = rec.get("error", "")
                yield done_event
                emitted += 1
                last_yield_ts = asyncio.get_event_loop().time()

            # Termination?
            if status in _TERMINAL:
                yield {
                    "type": "batch_done",
                    "plan_id": self.plan_id,
                    "status": status,
                    "total": total,
                    "success": counts.get("success", 0),
                    "failed": counts.get("failed", 0),
                }
                return

            # Still running — keep tailing DB until something changes.
            # If silent for too long, yield a heartbeat to push SSE keepalive bytes to the
            # client, so intermediate reverse proxies don't kill the connection as idle
            # during long LLM calls.
            now = asyncio.get_event_loop().time()
            if now - last_yield_ts >= _HEARTBEAT_INTERVAL_SEC:
                yield {
                    "type": "batch_heartbeat",
                    "plan_id": self.plan_id,
                    "ts": int(now),
                }
                last_yield_ts = now
            await asyncio.sleep(_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# TTL housekeeping (called by a startup task or cron — best effort)
# ---------------------------------------------------------------------------


def cleanup_expired_plans() -> int:
    """Delete plans whose expires_at has passed. Returns count deleted."""
    now = datetime.utcnow()
    with SessionLocal() as db:
        expired = (
            db.query(BatchPlan)
            .filter(BatchPlan.expires_at.isnot(None))
            .filter(BatchPlan.expires_at < now)
            .all()
        )
        n = len(expired)
        for p in expired:
            db.delete(p)
        db.commit()
        return n


__all__ = ["BatchOrchestrator", "cleanup_expired_plans"]

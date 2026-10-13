"""Run and resume a batch with one persisted project environment snapshot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.config.catalog import get_enabled_ids
from core.config.catalog_resolver import resolve_all_runtime_enabled
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.engine import SessionLocal
from core.infra.time import utc_now
from orchestration.batch_item_executor import _run_item_via_workflow

logger = logging.getLogger(__name__)


async def _run_until_done(plan_id: str, user_id: str) -> None:
    """Process unfinished items one by one, persisting results to DB.

    Idempotent: if the task is restarted (e.g. process restart), it
    resumes from len(progress.results) since already-stored entries are
    skipped on the next iteration.
    """
    from orchestration.batch_orchestrator import (
        _TERMINAL,
        CANCELLED,
        DONE,
        FAILED,
        RUNNING,
        _append_result,
        _batch_item_attempt_run_id,
        _blocking_item_run,
        _get_status,
        _load_plan,
        _read_results,
        _safe_format,
        _set_status,
        _summarize_item,
    )

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
        plan_chat_id: Optional[str] = plan.chat_id
        saved_environment = (plan.progress or {}).get("project_environment")
        # Mark running on first launch, idempotent on resume.
        if plan.status not in _TERMINAL:
            plan.status = RUNNING
            plan.updated_at = utc_now()
            db.commit()

    from core.llm.factory.request import AgentRequest
    from core.llm.project_environment import bind_project_environment

    try:
        environment_request = AgentRequest(
            current_user_id=user_id,
            chat_id=plan_chat_id,
            project_ctx=saved_environment,
            isolated=True,
        )
        await bind_project_environment(environment_request)
        project_ctx = environment_request.project_ctx
        with SessionLocal() as db:
            plan = _load_plan(db, plan_id)
            if plan is None:
                return
            db.refresh(plan, with_for_update=True)
            progress = dict(plan.progress or {})
            for count in ("done", "success", "failed"):
                progress.setdefault(count, 0)
            if "project_environment" not in progress:
                progress["project_environment"] = project_ctx
            project_ctx = progress["project_environment"]
            progress.pop("environment_error", None)
            plan.progress = progress
            db.commit()
        # Another runner may have persisted the first snapshot while this
        # runner resolved its environment. Always adopt that winning snapshot.
        environment_request = AgentRequest(
            current_user_id=user_id,
            chat_id=plan_chat_id,
            project_ctx=project_ctx,
            isolated=True,
        )
        await bind_project_environment(environment_request)
        project_ctx = environment_request.project_ctx
    except Exception as exc:
        logger.exception("[batch] environment initialization failed plan=%s", plan_id)
        with SessionLocal() as db:
            plan = _load_plan(db, plan_id)
            if plan is not None:
                db.refresh(plan, with_for_update=True)
                progress = dict(plan.progress or {})
                progress["environment_error"] = getattr(exc, "detail", None) or str(exc)
                plan.progress = progress
                plan.status = FAILED
                plan.updated_at = utc_now()
                db.commit()
        return

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
            _skills, _, _mcps = resolve_all_runtime_enabled(_cap_db, user_id)
        except Exception as _exc:  # pragma: no cover - defensive
            logger.warning("[batch] capability resolve failed plan=%s: %s", plan_id, _exc)
            _skills, _, _mcps = None, None, None
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
        blocked_run_id = _blocking_item_run(plan_id, next_idx)
        if blocked_run_id is not None:
            logger.error(
                "[batch] plan=%s item=%d blocked by unresolved run=%s",
                plan_id,
                next_idx,
                blocked_run_id,
            )
            _set_status(plan_id, FAILED)
            return

        item = items[next_idx]
        item_summary = _summarize_item(item, source_type)
        logger.info(
            "[batch] plan=%s starting item %d/%d (%s)",
            plan_id,
            next_idx,
            len(items),
            item_summary,
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
                prompt = _safe_format(template, item if isinstance(item, dict) else {"item": item})
                from core.services.run_journal import RunAlreadyExists, durable_run_binding

                try:
                    async with durable_run_binding(
                        user_id=user_id,
                        chat_id=plan_chat_id,
                        kind="batch_item",
                        external_id=f"{plan_id}:{next_idx}:{attempt}",
                        request_payload={"plan_id": plan_id, "item_index": next_idx},
                        recovery_snapshot={
                            "worker_args": {
                                "context": {
                                    **(project_ctx or {}),
                                    "project_ctx": project_ctx,
                                    "mcp_ids": list(sub_mcp_ids),
                                    "skill_ids": list(sub_skill_ids or []),
                                    "model_name": DEFAULT_CHAT_MODEL_ALIAS,
                                    "chat_mode": "fast",
                                }
                            }
                        },
                        binding_run_id=_batch_item_attempt_run_id(
                            user_id,
                            plan_id,
                            next_idx,
                            attempt,
                        ),
                    ) as binding:
                        accumulated_text, item_tool_calls, item_artifacts, item_citations = (
                            await _run_item_via_workflow(
                                prompt,
                                user_id,
                                sub_mcp_ids,
                                chat_id=binding.chat_id,
                                run_id=binding.run_id,
                                journal_owner=binding.owner,
                                sub_skill_ids=sub_skill_ids,
                                sub_visible_agents=sub_visible_agents,
                                project_ctx=project_ctx,
                            )
                        )
                except RunAlreadyExists as exc:
                    logger.info(
                        "[batch] plan=%s item=%d attempt=%d already admitted as run=%s",
                        plan_id,
                        next_idx,
                        attempt,
                        exc,
                    )
                    # Another process won the database admission race. It owns
                    # result persistence and terminal plan status; this runner
                    # must become a passive follower rather than overwrite it.
                    return
                success_text = accumulated_text.strip()
                break
            except asyncio.CancelledError:
                # The user cancelled (/cancel) or upstream really cancelled us —
                # let CancelledError bubble all the way up to _run_until_done, so the
                # task enters the cancelled state and done_callback records the reason.
                # Never translate the cancel into "skipped" here, or the remaining
                # items would keep running after a user cancellation.
                raise
            except Exception as exc:
                from core.services.tool_effect_ledger import find_tool_outcome_unknown

                if find_tool_outcome_unknown(exc) is not None:
                    logger.error(
                        "[batch] plan=%s item=%d paused for tool-effect recovery",
                        plan_id,
                        next_idx,
                    )
                    _set_status(plan_id, FAILED)
                    return
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "[batch] plan=%s item %d attempt %d failed: %s",
                    plan_id,
                    next_idx,
                    attempt,
                    last_error,
                )
                if attempt < max_retries:
                    await asyncio.sleep(min(2**attempt, 8))

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
            plan_id,
            next_idx,
            len(items),
            record.get("status"),
            counts_after.get("done", 0),
            len(items),
        )

    # All items processed (or items list was empty). Mark done unless
    # we were cancelled in the meantime.
    final_status = CANCELLED if _get_status(plan_id) == CANCELLED else DONE
    _set_status(plan_id, final_status)
    logger.info("[batch] plan=%s runner finished status=%s", plan_id, final_status)

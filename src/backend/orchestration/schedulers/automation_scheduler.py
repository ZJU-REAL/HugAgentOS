"""Automation scheduler — polls the DB for due tasks and fires them.

Inspired by claude-code's CronScheduler:
- Polls every 15s for due tasks
- Uses Redis distributed lock to prevent double-firing across instances
- Handles missed tasks on startup
- Auto-disables tasks after consecutive failure threshold
- Writes notifications to Redis for frontend polling
"""

import asyncio
import contextlib
import json
import os
import random
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _load_run_files(chat_id: str) -> List[Tuple[bytes, str, str]]:
    """Load the artifact files an automation run generated under its chat_id; returns a list of (bytes, filename, mime).

    Each run uses a brand-new chat_id (see _execute_*_task), and only pinned artifacts
    land in the Artifact table, so whatever is fetched by chat_id is exactly this run's
    deliverables. Used by channel delivery (Feishu etc.) to actually send out the
    generated documents. Best-effort.
    """
    from core.db.engine import SessionLocal
    from core.db.models import Artifact
    from core.storage import get_storage

    out: List[Tuple[bytes, str, str]] = []
    try:
        with SessionLocal() as db:
            storage = get_storage()
            rows = db.query(Artifact).filter(Artifact.chat_id == chat_id).all()
            for row in rows:
                if not row.storage_key:
                    continue
                try:
                    content = storage.download_bytes(row.storage_key)
                except Exception:  # noqa: BLE001
                    logger.warning("[scheduler] 产物下载失败 %s", row.artifact_id, exc_info=True)
                    continue
                out.append(
                    (
                        content,
                        row.filename or row.artifact_id,
                        row.mime_type or "application/octet-stream",
                    )
                )
    except Exception:  # noqa: BLE001
        logger.warning("[scheduler] 加载 run 产物失败 chat_id=%s", chat_id, exc_info=True)
    return out


POLL_INTERVAL_SECONDS = 15
REDIS_LOCK_PREFIX = "jx:auto:lock:"
REDIS_LOCK_TTL = 1800  # 30 minutes max lock hold (> TASK_EXECUTION_TIMEOUT_S)
# Max wall-clock per task execution. Must be < REDIS_LOCK_TTL so the
# timeout fires before the lock expires (otherwise scheduler could
# fire a parallel run while the previous one is still running).
# Heavy tasks (multi-domain search + full Word generation) legitimately
# run ~13 min and were getting killed at the old 800s ceiling, so this
# is raised to 25 min.
TASK_EXECUTION_TIMEOUT_S = 1500
# Runs older than this in 'running' state on startup are treated as
# orphaned (killed by OOM/restart) and recovered to 'failed'. Kept above
# TASK_EXECUTION_TIMEOUT_S so a live run is never falsely recovered.
STUCK_RUNNING_THRESHOLD_S = 2400  # 40 minutes

_scheduler_instance: Optional["AutomationScheduler"] = None


def get_scheduler() -> Optional["AutomationScheduler"]:
    return _scheduler_instance


class AutomationScheduler:
    """Async scheduler that polls the DB for due tasks and fires them."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        global _scheduler_instance
        _scheduler_instance = self
        logger.info("[scheduler] started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        global _scheduler_instance
        _scheduler_instance = None
        logger.info("[scheduler] stopped")

    async def _poll_loop(self):
        # Small initial delay to let the app finish startup
        await asyncio.sleep(5)

        # Recover stuck 'running' rows from previous OOM/restart first
        await self._recover_stuck_running_runs()
        # Then handle missed one-shot tasks
        await self._recover_missed_tasks()

        while self._running:
            try:
                await self._check_and_fire()
            except Exception as e:
                logger.error("[scheduler] poll error: %s", e, exc_info=True)
            jitter = random.uniform(0, 5)
            await asyncio.sleep(POLL_INTERVAL_SECONDS + jitter)

    async def _check_and_fire(self):
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        now = datetime.utcnow().replace(tzinfo=pytz.utc)
        with SessionLocal() as db:
            svc = AutomationService(db)
            due_tasks = svc.get_due_tasks(now)

        if not due_tasks:
            return

        logger.info("[scheduler] found %d due tasks", len(due_tasks))
        for task in due_tasks:
            # Try to acquire Redis distributed lock
            acquired = await self._acquire_lock(task.task_id)
            if not acquired:
                continue
            # Pre-advance next_run_at BEFORE firing so the schedule moves
            # on regardless of whether the run itself succeeds, fails, or
            # gets killed mid-flight. Mirrors how real cron behaves and
            # prevents the death-spiral where a stuck "running" row leaves
            # next_run_at in the past forever and causes every poll to
            # re-fire the same task. The success/failure branches in
            # execute_task no longer call advance_next_run.
            try:
                with SessionLocal() as db:
                    svc = AutomationService(db)
                    svc.advance_next_run(task.task_id)
            except Exception as exc:
                logger.warning(
                    "[scheduler] pre-advance failed for %s: %s — firing anyway",
                    task.task_id,
                    exc,
                )
            # Fire in background
            asyncio.create_task(self.execute_task(task.task_id, task.user_id))

    async def execute_task(self, task_id: str, user_id: str):
        """Execute a single scheduled task."""
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        start = time.monotonic()

        with SessionLocal() as db:
            svc = AutomationService(db)
            task = svc.get_task_by_id(task_id)
            if not task or task.status not in ("active", "paused"):
                await self._release_lock(task_id)
                return

            run = svc.record_run_start(task_id)
            task_type = task.task_type
            task_name = task.name or "定时任务"
            task_prompt = task.prompt
            task_plan_id = task.plan_id
            task_consecutive_failures = task.consecutive_failures or 0
            task_max_failures = task.max_failures or 3
            # Pass `None` (not `[]`) when the task wasn't configured with
            # explicit IDs so that plan-mode falls back to plan-declared
            # expected_* lists. An explicit empty list now means "strictly
            # no tools/skills/agents".
            enabled_mcp_ids = task.enabled_mcp_ids or None
            enabled_skill_ids = task.enabled_skill_ids or None
            enabled_kb_ids = task.enabled_kb_ids or None
            enabled_agent_ids = task.enabled_agent_ids or None
            task_metadata = dict(task.extra_data or {})  # includes optional channel delivery destinations

        try:
            if task_type == "prompt":
                chat_id, result_summary, usage = await asyncio.wait_for(
                    self._execute_prompt_task(
                        user_id=user_id,
                        task_name=task_name,
                        prompt=task_prompt,
                        task_id=task_id,
                        enabled_mcp_ids=enabled_mcp_ids,
                        enabled_skill_ids=enabled_skill_ids,
                        enabled_kb_ids=enabled_kb_ids,
                    ),
                    timeout=TASK_EXECUTION_TIMEOUT_S,
                )
            elif task_type == "plan":
                chat_id, result_summary, usage = await asyncio.wait_for(
                    self._execute_plan_task(
                        user_id=user_id,
                        task_name=task_name,
                        plan_id=task_plan_id,
                        task_id=task_id,
                        enabled_mcp_ids=enabled_mcp_ids,
                        enabled_skill_ids=enabled_skill_ids,
                        enabled_kb_ids=enabled_kb_ids,
                        enabled_agent_ids=enabled_agent_ids,
                    ),
                    timeout=TASK_EXECUTION_TIMEOUT_S,
                )
            elif task_type == "loop":
                # Periodically advance a persistent autonomous loop (M4 scheduler integration); loop_id stored in task.extra_data
                chat_id, result_summary, usage = await asyncio.wait_for(
                    self._execute_loop_task(
                        user_id=user_id,
                        task_name=task_name,
                        loop_id=(task_metadata or {}).get("loop_id"),
                    ),
                    timeout=TASK_EXECUTION_TIMEOUT_S,
                )
            else:
                raise ValueError(f"Unknown task type: {task_type}")

            duration_ms = int((time.monotonic() - start) * 1000)

            with SessionLocal() as db:
                svc = AutomationService(db)
                svc.record_run_complete(
                    run.run_id,
                    status="success",
                    chat_id=chat_id,
                    result_summary=result_summary,
                    duration_ms=duration_ms,
                    usage=usage,
                )
                svc.update_task_system(
                    task_id,
                    consecutive_failures=0,
                    last_run_at=datetime.utcnow(),
                    sidebar_activated=True,
                )
                # next_run_at already pre-advanced in _check_and_fire; now that
                # the run actually finished, flip one-shot/exhausted tasks to
                # "completed" (advance_next_run intentionally left them active so
                # this run could pass the execute_task guard).
                svc.finalize_after_run(task_id)

            # Multi-target delivery (delivery_targets model, with backward compat for the old flat channel_id/conversation_id).
            # In-app (notification center + sidebar + chat history) is delivered only when targets include inapp; channels and other outbound targets are delivered one by one.
            from core.services.delivery_targets import resolve_delivery_targets, has_inapp

            _targets = resolve_delivery_targets(task_metadata)
            if has_inapp(_targets):
                await self._send_notification(
                    user_id, task_id, task_name, "success", result_summary or "执行完成", chat_id
                )
            logger.info("[scheduler] task %s completed in %dms", task_id, duration_ms)

            # Artifact files generated by this run (pinned deliverables anchored under this run's chat_id).
            # Loaded once and reused across all channel targets — otherwise "generate a document and send it to Feishu" only sends the text and the file never goes out.
            _gen_files = _load_run_files(chat_id) if chat_id else []
            for _tgt in _targets:
                if _tgt.get("type") != "channel":
                    continue  # inapp already handled; email etc. reserved
                _ch = _tgt.get("channel_id")
                _conv = _tgt.get("conversation_id")
                if not (_ch and _conv):
                    continue
                try:
                    from core.channels.outbound import deliver_to_conversation

                    head = f"【{task_name}】\n" if task_name else ""
                    _ok = await deliver_to_conversation(
                        _ch,
                        _conv,
                        head + (result_summary or "执行完成"),
                        files=_gen_files,
                    )
                    if _ok:
                        logger.info(
                            "[scheduler] 渠道投递成功 task=%s channel=%s conv=%s files=%d",
                            task_id, _ch, _conv, len(_gen_files),
                        )
                    else:
                        logger.warning(
                            "[scheduler] 渠道投递失败（原因见 [channels] 日志）task=%s channel=%s conv=%s",
                            task_id, _ch, _conv,
                        )
                except Exception:
                    logger.warning("[scheduler] 渠道投递异常 task=%s", task_id, exc_info=True)

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            error_msg = str(e)[:2000]
            logger.error("[scheduler] task %s failed: %s", task_id, error_msg, exc_info=True)

            with SessionLocal() as db:
                svc = AutomationService(db)
                svc.record_run_complete(
                    run.run_id,
                    status="failed",
                    error_message=error_msg,
                    duration_ms=duration_ms,
                )
                new_failures = task_consecutive_failures + 1
                updates: Dict[str, Any] = {
                    "consecutive_failures": new_failures,
                    "last_error": error_msg,
                    "last_run_at": datetime.utcnow(),
                    "sidebar_activated": True,
                }
                if new_failures >= task_max_failures:
                    updates["status"] = "disabled"
                    logger.warning(
                        "[scheduler] task %s auto-disabled after %d failures", task_id, new_failures
                    )
                svc.update_task_system(task_id, **updates)
                # next_run_at already pre-advanced in _check_and_fire. Finalize
                # one-shot tasks so a failed single run lands in a terminal state
                # instead of dangling active (finalize_after_run skips disabled).
                svc.finalize_after_run(task_id)

            await self._send_notification(user_id, task_id, task_name, "failed", error_msg[:200])

        finally:
            await self._release_lock(task_id)

    async def _execute_prompt_task(
        self,
        *,
        user_id: str,
        task_name: str,
        prompt: str,
        task_id: str,
        enabled_mcp_ids: Optional[List[str]],
        enabled_skill_ids: Optional[List[str]],
        enabled_kb_ids: Optional[List[str]],
    ) -> Tuple[str, str, Dict]:
        """Execute a prompt-type task.

        Mirrors the stream-consumption behaviour of the normal chat endpoint
        (api/routes/v1/chats.py:chat_stream) so that tool_calls, artifacts,
        citations, sources and warnings are all persisted to the chat message,
        and generated files are written into the Artifact table. Without this,
        automation chats show only bare text without any file attachments or
        tool-call history.
        """
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService
        from orchestration.workflow import astream_chat_workflow
        from core.chat.tool_log import (
            attach_tool_result as _attach_tool_result,
            upsert_tool_call as _upsert_tool_call,
        )
        from core.services.artifact_service import persist_artifacts as _persist_artifacts
        from core.llm import workspace as _workspace_mod

        chat_id = f"chat_{uuid.uuid4().hex[:16]}"
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        from core.services.user_model_selection import resolve_effective_chat_model_name

        actual_model_name = resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS

        with SessionLocal() as db:
            chat_svc = ChatService(db)
            chat_svc.ensure_session(
                chat_id=chat_id,
                user_id=user_id,
                title=f"[自动化] {task_name}",
                extra_data={"automation_task_id": task_id, "automation_run": True},
            )
            chat_svc.add_message(
                chat_id=chat_id,
                role="user",
                content=prompt,
                model=actual_model_name,
            )

        # Align the capability set with the user: when the task has no explicit config,
        # resolve the full set of capabilities currently available to that user
        # (global catalog + private/plugin marketplace installs), same source as the web
        # UI and channel inbound. Otherwise falling back to the catalog default set
        # misses the user's installed plugin skills/MCPs (root cause of "plugin XX not found").
        enabled_agent_ids: Optional[List[str]] = None
        if enabled_skill_ids is None or enabled_mcp_ids is None:
            from core.config.catalog_resolver import resolve_all_runtime_enabled

            with SessionLocal() as db:
                u_skills, u_agents, u_mcps = resolve_all_runtime_enabled(db, user_id)
            if enabled_skill_ids is None:
                enabled_skill_ids = u_skills
            if enabled_mcp_ids is None:
                enabled_mcp_ids = u_mcps
            enabled_agent_ids = u_agents

        context = {
            "user_id": user_id,
            "chat_id": chat_id,
            "model_name": actual_model_name,
            "enable_thinking": False,
            "memory_enabled": False,
            # The keys the workflow side reads are enabled_skills / enabled_mcps / enabled_kbs
            # (catalog_resolver's *_from_context); the old keys enabled_*_ids were never
            # consumed, which made the task's explicitly configured capability list a no-op.
            "enabled_mcps": enabled_mcp_ids,
            "enabled_skills": enabled_skill_ids,
            "enabled_kbs": enabled_kb_ids,
            "enabled_agents": enabled_agent_ids,
        }
        session_messages = [{"role": "user", "content": prompt}]

        full_response = ""
        usage: Dict = {}
        tool_calls_log: List[Dict[str, Any]] = []
        meta_fields: Dict[str, Any] = {}
        # Strict workspace gate.
        _workspace_mod.init_state()

        async for chunk in astream_chat_workflow(
            session_messages=session_messages,
            user_message=prompt,
            context=context,
        ):
            chunk_type = chunk.get("type")

            if chunk_type in {"content", "ai_message"}:
                full_response += chunk.get("delta", "")

            elif chunk_type == "tool_call":
                tc: Dict[str, Any] = {
                    "tool_name": chunk.get("tool_name"),
                    "tool_display_name": chunk.get("tool_display_name"),
                    "tool_args": chunk.get("tool_args", {}),
                    "tool_id": chunk.get("tool_id"),
                }
                if chunk.get("subagent_name"):
                    tc["subagent_name"] = chunk["subagent_name"]
                _upsert_tool_call(tool_calls_log, tc)

            elif chunk_type == "tool_result":
                _tid = chunk.get("tool_id")
                _tn = chunk.get("tool_name")
                _res = chunk.get("result", {})
                _attach_tool_result(tool_calls_log, _tid, _tn, _res)

            elif chunk_type == "meta":
                usage = chunk.get("usage", {}) or {}
                _ws_pinned = _workspace_mod.get_pinned()
                meta_fields = {
                    "route": chunk.get("route", "main"),
                    "sources": chunk.get("sources", []),
                    "artifacts": _ws_pinned,
                    "workspace_files": _workspace_mod.get_pinned_file_ids(),
                    "warnings": chunk.get("warnings", []),
                    "is_markdown": chunk.get("is_markdown", True),
                    "citations": chunk.get("citations", []),
                }

        # Persist assistant message with the full run context.
        with SessionLocal() as db:
            chat_svc = ChatService(db)
            chat_svc.add_message(
                chat_id=chat_id,
                role="assistant",
                content=full_response,
                model=actual_model_name,
                message_id=message_id,
                tool_calls=tool_calls_log if tool_calls_log else None,
                usage=usage,
                extra_data={
                    "timestamp": datetime.utcnow().isoformat(),
                    "route": meta_fields.get("route", "main"),
                    "is_markdown": meta_fields.get("is_markdown", True),
                    "sources": meta_fields.get("sources", []),
                    "artifacts": meta_fields.get("artifacts", []),
                    "workspace_files": meta_fields.get("workspace_files", []),
                    "warnings": meta_fields.get("warnings", []),
                    "citations": meta_fields.get("citations", []),
                    "message_id": message_id,
                    "automation_task_id": task_id,
                    "automation_run": True,
                },
            )
            # Strict workspace gate: only pinned files reach My Space (我的空间).
            # Pass scope explicitly — today automation is not attached to projects
            # (context has no project_id), so project_scope_from_context returns None;
            # but keep the API consistent with chat_stream, guarding against re-hitting
            # the "forgot to pass scope" pitfall if automation gets project mode later.
            from core.services.project_scope import project_scope_from_context

            _persist_artifacts(
                db,
                user_id,
                chat_id,
                _workspace_mod.get_pinned(),
                scope=project_scope_from_context(context),
            )

        summary = full_response[:500] if full_response else "执行完成"
        return chat_id, summary, usage

    async def _execute_plan_task(
        self,
        *,
        user_id: str,
        task_name: str,
        plan_id: str,
        task_id: str,
        enabled_mcp_ids: Optional[List[str]],
        enabled_skill_ids: Optional[List[str]],
        enabled_kb_ids: Optional[List[str]],
        enabled_agent_ids: Optional[List[str]],
    ) -> Tuple[str, str, Dict]:
        """Execute a plan-type task.

        Produces a real chat session with a plan_snapshot assistant message so
        "查看对话" in the run history resolves to a loadable conversation.
        """
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService
        from core.services.plan_service import PlanService
        from core.services.artifact_service import persist_artifacts as _persist_artifacts
        from core.services.user_model_selection import resolve_effective_chat_model_name
        from orchestration.subagents.plan_mode import astream_execute_plan

        chat_id = f"chat_{uuid.uuid4().hex[:16]}"
        actual_model_name = resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS

        with SessionLocal() as db:
            plan_svc = PlanService(db)
            plan = plan_svc.get_plan(plan_id, user_id)
            if not plan:
                raise ValueError(f"Plan {plan_id} not found")

            plan_title = plan.title

            if plan.status in ("completed", "failed", "cancelled"):
                plan_svc.update_plan(plan_id, status="approved", completed_steps=0)
                for step in plan.steps:
                    plan_svc.update_step(
                        step.step_id,
                        status="pending",
                        result_summary=None,
                        ai_output=None,
                        error_message=None,
                    )
            elif plan.status == "draft":
                plan_svc.update_plan(plan_id, status="approved")

            chat_svc = ChatService(db)
            chat_svc.ensure_session(
                chat_id=chat_id,
                user_id=user_id,
                title=f"[自动化] {task_name}",
                extra_data={
                    "automation_task_id": task_id,
                    "automation_run": True,
                    "plan_chat": True,
                    "plan_id": plan_id,
                },
            )
            chat_svc.add_message(
                chat_id=chat_id,
                role="user",
                content=f"自动化执行计划：{plan_title}",
                model=actual_model_name,
            )

        from core.llm import workspace as _workspace_mod

        result_text = ""
        usage: Dict = {}
        completed_steps = 0
        total_steps = 0
        tool_calls_log: List[Dict[str, Any]] = []
        _workspace_mod.init_state()

        with SessionLocal() as db:
            async for event in astream_execute_plan(
                plan_id=plan_id,
                user_id=user_id,
                db=db,
                enabled_mcp_ids=enabled_mcp_ids,
                enabled_skill_ids=enabled_skill_ids,
                enabled_kb_ids=enabled_kb_ids,
                enabled_agent_ids=enabled_agent_ids,
                chat_id=chat_id,
                model_name=actual_model_name,
            ):
                evt_type = event.get("type")
                if evt_type == "plan_complete":
                    result_text = event.get("result_text", "")
                    completed_steps = event.get("completed_steps", 0)
                    total_steps = event.get("total_steps", 0)
                    usage = event.get("usage", {}) or {}
                elif evt_type == "tool_call":
                    tool_calls_log.append(
                        {
                            "tool_name": event.get("tool_name"),
                            "tool_id": event.get("tool_id"),
                            "tool_args": event.get("tool_args", {}),
                            "step_id": event.get("step_id"),
                        }
                    )
                elif evt_type == "tool_result":
                    _tid = event.get("tool_id")
                    _tn = event.get("tool_name")
                    result = event.get("result")
                    matched = False
                    for _tc in tool_calls_log:
                        if _tid and _tc.get("tool_id") == _tid and "result" not in _tc:
                            _tc["result"] = result
                            _tc["status"] = "success"
                            matched = True
                            break
                    if not matched:
                        tool_calls_log.append(
                            {
                                "tool_name": _tn,
                                "tool_id": _tid,
                                "result": result,
                                "status": "success",
                                "step_id": event.get("step_id"),
                            }
                        )
        with SessionLocal() as db:
            plan_svc = PlanService(db)
            updated_plan = plan_svc.get_plan(plan_id, user_id)
            plan_snapshot: Optional[Dict[str, Any]] = None
            if updated_plan:
                plan_snapshot = PlanService.build_execution_snapshot(
                    updated_plan,
                    completed_steps=completed_steps,
                    total_steps=total_steps,
                    result_text=result_text,
                )

            # Strict workspace gate: pinned-only.
            _ws_pinned = _workspace_mod.get_pinned()
            _ws_files = _workspace_mod.get_pinned_file_ids()

            assistant_content = result_text or (
                f"计划执行完成：共 {total_steps} 步，完成 {completed_steps} 步。"
            )
            chat_svc = ChatService(db)
            chat_svc.add_message(
                chat_id=chat_id,
                role="assistant",
                content=assistant_content,
                model=actual_model_name,
                extra_data={
                    "is_markdown": bool(result_text),
                    "plan_id": plan_id,
                    "plan_snapshot": plan_snapshot,
                    "artifacts": _ws_pinned,
                    "workspace_files": _ws_files,
                    "completed_steps": completed_steps,
                    "total_steps": total_steps,
                    "automation_task_id": task_id,
                    "automation_run": True,
                },
                tool_calls=tool_calls_log if tool_calls_log else None,
                usage=usage,
            )
            # Chats created by automation are not attached to projects -> scope is None;
            # keep the explicit construction, guarding against re-hitting the "forgot to
            # pass scope" pitfall when project mode is wired in later.
            from core.services.project_scope import project_scope_from_chat_id

            _persist_artifacts(
                db,
                user_id,
                chat_id,
                _ws_pinned,
                scope=project_scope_from_chat_id(db, chat_id),
            )

        summary = (result_text or assistant_content)[:500]
        return chat_id, summary, usage

    async def _execute_loop_task(self, *, user_id: str, task_name: str, loop_id):
        """Periodically advance a persistent autonomous loop: start/resume a loop run and wait for it to reach a terminal state (M4 scheduler integration).

        Concurrency guard: if the loop already has an active run, skip this trigger
        (avoids duplicate advancement within the same session). The driver auto-resumes
        from feature_list.json, so each tick = one segment of progress.
        """
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService
        from core.services.loop_service import LoopService
        from orchestration import chat_run_executor

        if not loop_id:
            raise ValueError("loop 任务缺少 extra_data.loop_id")

        with SessionLocal() as db:
            loop = LoopService(db).get_loop(loop_id)
            if not loop:
                raise ValueError(f"loop {loop_id} not found")
            if loop.status in ("completed", "cancelled"):
                return None, f"loop 已终态({loop.status})，跳过", {}
            chat_id = loop.chat_id or f"loopchat_{loop_id}"
            goal_spec = dict(loop.goal_spec or {})
            budget = dict(loop.budget or {})
            try:
                ChatService(db).ensure_session(
                    chat_id=chat_id, user_id=user_id,
                    title=f"[自主循环] {loop.title or task_name}",
                    extra_data={"autonomous_loop": True, "loop_id": loop_id},
                )
                if not loop.chat_id:
                    loop.chat_id = chat_id
                    db.commit()
            except Exception:  # noqa: BLE001
                pass

        # Concurrency guard: active run already exists -> skip
        if chat_run_executor.get_active_run_for_chat(chat_id, user_id):
            return chat_id, "loop 已在推进中，跳过本次触发", {}

        run = await chat_run_executor.start_autonomous_loop_run(
            loop_id=loop_id, chat_id=chat_id, user_id=user_id,
            goal_spec=goal_spec, budget=budget,
        )
        # Wait for it to reach a terminal state (bounded by the outer TASK_EXECUTION_TIMEOUT_S; on timeout the run continues in the background and the next tick resumes it)
        task = chat_run_executor._active_runs.get(run.run_id)
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(task)

        with SessionLocal() as db:
            loop = LoopService(db).get_loop(loop_id)
            summary = (
                f"loop status={loop.status} score={loop.final_score} iters={loop.iteration_count}"
                if loop else "loop 推进完成"
            )
            usage = {"total_tokens": (loop.tokens_spent if loop else 0)}
        return chat_id, summary, usage

    async def _recover_stuck_running_runs(self):
        """Mark long-running 'running' runs as failed (orphaned by OOM/restart).

        Without this, the DB accumulates rows in 'running' state from
        runs that were killed mid-flight, and (worse) the parent task's
        next_run_at can stay in the past — causing every poll to immediately
        re-fire the same task in a death spiral.
        """
        from core.db.engine import SessionLocal
        from core.db.models import ScheduledTaskRun, ScheduledTask
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=STUCK_RUNNING_THRESHOLD_S)
        try:
            with SessionLocal() as db:
                stuck = (
                    db.query(ScheduledTaskRun)
                    .filter(
                        ScheduledTaskRun.status == "running",
                        ScheduledTaskRun.started_at < cutoff,
                    )
                    .all()
                )
                stuck_task_ids = set()
                for run in stuck:
                    run.status = "failed"
                    run.error_message = "interrupted (orphan, recovered on startup)"
                    run.completed_at = datetime.now(timezone.utc)
                    stuck_task_ids.add(run.task_id)

                # Push parent tasks' next_run_at past now so the next poll
                # doesn't immediately re-fire the same hung task.
                from core.services.automation_service import AutomationService

                svc = AutomationService(db)
                for tid in stuck_task_ids:
                    task = svc.get_task_by_id(tid)
                    if task and task.next_run_at:
                        try:
                            svc.advance_next_run(tid)
                        except Exception as exc:
                            logger.warning(
                                "[scheduler] startup advance failed for %s: %s",
                                tid,
                                exc,
                            )
                    # One-shot tasks pre-advance to next_run_at=None, so the
                    # branch above skips them; finalize so an interrupted single
                    # run doesn't dangle 'active' with no next_run_at forever.
                    try:
                        svc.finalize_after_run(tid)
                    except Exception as exc:
                        logger.warning(
                            "[scheduler] startup finalize failed for %s: %s",
                            tid,
                            exc,
                        )

                if stuck:
                    db.commit()
                    logger.info(
                        "[scheduler] recovered %d stuck 'running' runs across %d tasks",
                        len(stuck),
                        len(stuck_task_ids),
                    )
        except Exception as exc:
            logger.error("[scheduler] stuck-runs recovery failed: %s", exc, exc_info=True)

    async def _recover_missed_tasks(self):
        """On startup, check for one-shot tasks whose next_run_at is in the past."""
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        now = datetime.utcnow().replace(tzinfo=pytz.utc)
        try:
            with SessionLocal() as db:
                svc = AutomationService(db)
                missed = svc.get_due_tasks(now)
                one_shot_count = 0
                for task in missed:
                    if not task.recurring:
                        one_shot_count += 1
                        asyncio.create_task(self.execute_task(task.task_id, task.user_id))
                if one_shot_count:
                    logger.info("[scheduler] recovering %d missed one-shot tasks", one_shot_count)
        except Exception as e:
            logger.error("[scheduler] recovery error: %s", e)

    # ── Redis lock helpers ─────────────────────────────────────────

    async def _acquire_lock(self, task_id: str) -> bool:
        try:
            from core.infra.redis import get_redis

            redis = get_redis()
            key = f"{REDIS_LOCK_PREFIX}{task_id}"
            result = await redis.set(key, "1", ex=REDIS_LOCK_TTL, nx=True)
            return bool(result)
        except Exception as e:
            logger.warning("[scheduler] lock acquire failed for %s: %s", task_id, e)
            return False

    async def _release_lock(self, task_id: str):
        try:
            from core.infra.redis import get_redis

            redis = get_redis()
            await redis.delete(f"{REDIS_LOCK_PREFIX}{task_id}")
        except Exception as e:
            logger.warning("[scheduler] lock release failed for %s: %s", task_id, e)

    # ── Notification ───────────────────────────────────────────────

    async def _send_notification(
        self,
        user_id: str,
        task_id: str,
        task_name: str,
        status: str,
        summary: str,
        chat_id: Optional[str] = None,
    ):
        try:
            from core.infra.redis import get_redis

            redis = get_redis()
            notification = {
                "id": f"notif_{uuid.uuid4().hex[:12]}",
                "task_id": task_id,
                "task_name": task_name,
                "status": status,
                "summary": summary[:200],
                "chat_id": chat_id,
                "timestamp": int(datetime.utcnow().timestamp() * 1000),
                "read": False,
            }
            key = f"jx:notifications:{user_id}"
            await redis.lpush(key, json.dumps(notification, ensure_ascii=False))
            await redis.ltrim(key, 0, 49)  # Keep latest 50
            await redis.expire(key, 7 * 24 * 3600)  # 7 day TTL
        except Exception as e:
            logger.warning("[scheduler] notification failed: %s", e)

"""Scheduled execution uses the same live run lifecycle as interactive conversations."""

import asyncio
import uuid
from typing import Dict, List, Optional, Tuple

import core.chat.runtime.admission as admission
import core.chat.runtime.handoff as handoff
import core.runs.runtime as runtime
import orchestration.loops.admission as _orchestration_loops_admission
import orchestration.plans.admission as _orchestration_plans_admission
from core.chat.context import build_runtime_context, resolve_enabled_capabilities
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db import engine as db_engine
from core.db.models import ChatMessage, ChatRun
from features.automation.models import ScheduledTaskRun
from features.automation.runs import owned_run_guard
from core.models import selection as user_model_selection
from features.automation.execution import task_execution_context
from features.automation.service import AutomationService
from core.chat.sequencer import ChatSequencer
from core.chat.service import ChatService
from core.db.threads import run_db, run_in_session


class ConversationStopped(Exception):
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        super().__init__("用户已停止执行")


def _prepare(
    user_id,
    task_id,
    task_name,
    content,
    model_name,
    automation_run_id,
    metadata=None,
    lease_token=None,
):
    chat_id = f"chat_{automation_run_id}" if automation_run_id else f"chat_{uuid.uuid4().hex[:16]}"
    with db_engine.SessionLocal() as db:
        task = AutomationService(db).get_task(task_id, user_id)
        if task is None:
            raise ValueError("任务不存在或无权访问")
        context = task_execution_context(db, task)
        service = ChatService(db)
        service.ensure_session(
            chat_id=chat_id,
            user_id=user_id,
            title=task_name,
            project_id=context.get("project_id"),
            extra_data={"automation_task_id": task_id, "automation_run": True, **(metadata or {})},
        )
        if content is not None:
            service.add_message(chat_id=chat_id, role="user", content=content, model=model_name)
    return chat_id, context


def _link_run(db, automation_run_id, task_id, chat_id, *, lease_token=None):
    if automation_run_id is None:
        return
    query = db.query(ScheduledTaskRun).filter(
        ScheduledTaskRun.run_id == automation_run_id,
        ScheduledTaskRun.task_id == task_id,
        ScheduledTaskRun.status == "running",
    )
    if lease_token is not None:
        query = query.filter(ScheduledTaskRun.lease_token == lease_token)
    if not query.update({ScheduledTaskRun.chat_id: chat_id}, synchronize_session=False):
        raise ValueError("定时任务执行记录不存在或已被接管")
    db.flush()


def _accept_link(run_id, task_id, token, recovery_snapshot=None):
    def link(db, child):
        _link_run(db, run_id, task_id, child.chat_id, lease_token=token)
        if recovery_snapshot is not None:
            import json
            child.recovery_snapshot = json.loads(json.dumps(recovery_snapshot, ensure_ascii=False, default=str))
            child.snapshot_version = 1
            child.run_phase = "accepted"
            child.last_operation_safety = "replayable"
    return link


async def _wait(run, *, automation_run_id=None, lease_token=None) -> Tuple[str, str, Dict]:
    """Observe durable state without taking ownership of the executor's asyncio tasks.

    Scheduler shutdown transfers observation; the shared executor owns the child run.
    User cancellation is an outcome, not cancellation of the scheduler itself.
    """
    try:
        while True:
            current = await asyncio.to_thread(runtime.get_run, run.run_id)
            if current is None:
                raise RuntimeError("会话执行记录不存在")
            if current.status == "pending":
                from orchestration.pending import dispatch_pending
                await dispatch_pending(current)
            if current.status not in ("pending", "running"):
                successor = await asyncio.to_thread(handoff.get_successor_run, run.run_id, user_id=run.user_id)
                if successor is not None:
                    run = successor
                    continue
                # A cancelled cooperative plan may still be releasing its writer.
                if current.status == "cancelled" and current.writer_slot:
                    await asyncio.sleep(1)
                    continue
                if current.status == "cancelled":
                    raise ConversationStopped(run.chat_id)
                if current.status != "completed":
                    raise RuntimeError(current.error_message or "会话执行未完成")
                return await asyncio.to_thread(_result, run)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # The durable child is owned by the shared run executor. Replacing an
        # observer must not cancel accepted work; recovery follows the same run.
        raise


def _result(run):
    with db_engine.SessionLocal() as db:
        durable = db.get(ChatRun, run.run_id)
        snapshot = durable.recovery_snapshot or {}
        message_id = (
            snapshot.get("message_id")
            or snapshot.get("worker_args", {}).get("context", {}).get("message_id")
            or run.message_id
        )
        message = db.get(ChatMessage, message_id)
        if message is None:
            raise RuntimeError("会话执行结果未保存")
        from core.infra.model_output import strip_think_blocks
        result = strip_think_blocks(message.content or "") or "执行完成"
        return run.chat_id, result, dict(message.usage or {})


def _find_child(chat_id, user_id, started_at):
    with db_engine.SessionLocal() as db:
        # Prompt/Plan use fresh chats; Loop reuses its chat. The first main run
        # after this invocation started is its root, not a later user message.
        run = (
            db.query(ChatRun)
            .filter(
                ChatRun.chat_id == chat_id,
                ChatRun.user_id == user_id,
                ChatRun.created_at >= started_at,
                ChatRun.request_payload["kind"]
                .as_string()
                .in_(("chat", "plan_execute", "autonomous_loop")),
            )
            .order_by(ChatRun.created_at.asc())
            .first()
        )
        if run is None:
            raise RuntimeError("定时任务关联的会话执行记录不存在")
    return run


async def resume_conversation(
    chat_id, user_id, started_at, *, automation_run_id=None, lease_token=None
):
    """Observe this invocation's original child and its durable successors."""
    run = await asyncio.to_thread(_find_child, chat_id, user_id, started_at)
    return await _wait(run, automation_run_id=automation_run_id, lease_token=lease_token)


def _accept_prompt_run(db, **kwargs):
    accepted = ChatSequencer(db).accept_main_run(**kwargs)
    db.refresh(accepted.run)
    db.expunge(accepted.run)
    return accepted.run


def _abandon_run(db, run_id, reason):
    ChatSequencer(db).abandon_pending_run(run_id, reason=reason)


def _approve_plan(db, task_id, user_id, plan_id):
    """Check the task and plan, re-arm a finished plan; returns the plan title."""
    from core.chat.plans import PlanService

    task = AutomationService(db).get_task(task_id, user_id)
    if task is None:
        raise ValueError("任务不存在或无权访问")
    task_execution_context(db, task)
    service = PlanService(db)
    plan = service.get_plan(plan_id, user_id)
    if plan is None:
        raise ValueError("计划不存在或无权访问")
    title = plan.title
    if plan.status in ("completed", "failed", "cancelled"):
        service.update_plan(plan_id, status="approved", completed_steps=0)
        for step in plan.steps:
            service.update_step(
                step.step_id,
                status="pending",
                result_summary=None,
                ai_output=None,
                error_message=None,
            )
    elif plan.status == "draft":
        service.update_plan(plan_id, status="approved")
    return title


def _loop_target(db, user_id, task_name, loop_id, task_id):
    """Prepare the loop's chat; returns a skip notice, or (chat_id, goal_spec, budget, project_context)."""
    from features.automation.loops import LoopService

    task = AutomationService(db).get_task(task_id, user_id)
    if task is None:
        raise ValueError("任务不存在或无权访问")
    project_context = task_execution_context(db, task)
    loop = LoopService(db).get_loop(loop_id)
    if loop is None or loop.user_id != user_id:
        raise ValueError("loop 不存在或无权访问")
    if loop.status in ("completed", "cancelled"):
        return f"loop 已终态({loop.status})，跳过"
    chat_id = loop.chat_id or f"loopchat_{loop_id}"
    goal_spec, budget = dict(loop.goal_spec or {}), dict(loop.budget or {})
    ChatService(db).ensure_session(
        chat_id=chat_id,
        user_id=user_id,
        title=task_name,
        project_id=project_context.get("project_id"),
        extra_data={
            "automation_task_id": task_id,
            "automation_run": True,
            "autonomous_loop": True,
            "loop_id": loop_id,
        },
    )
    if not loop.chat_id:
        loop.chat_id = chat_id
        db.commit()
    return chat_id, goal_spec, budget, project_context


async def execute_prompt(
    *,
    user_id: str,
    task_name: str,
    prompt: str,
    task_id: str,
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]] = None,
    automation_run_id: Optional[str] = None,
    lease_token: Optional[str] = None,
):
    model = user_model_selection.resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS
    chat_id, project_context = await run_db(
        _prepare, user_id, task_id, task_name, None, model, automation_run_id, lease_token=lease_token
    )
    def _enabled_capabilities(db):
        skills, agents, mcps = resolve_enabled_capabilities(
            db, user_id, enabled_skill_ids, enabled_agent_ids, enabled_mcp_ids
        )
        return mcps, agents, skills
    mcps, agents, skills = await run_in_session(_enabled_capabilities)
    from core.ontology.runtime_policy import build_user_ontology_runtime

    ontology_enabled, ontology_runtime = await run_db(
        build_user_ontology_runtime, user_id=user_id, task=prompt
    )
    context = build_runtime_context(
        model_name=model,
        enable_thinking=True,
        user_id=user_id,
        chat_id=chat_id,
        enabled_mcps=mcps,
        enabled_skills=skills,
        enabled_kbs=enabled_kb_ids,
        enabled_agents=agents,
    )
    context.update(
        {
            **project_context,
            "automation_run": True,
            "chat_mode": "medium",
            "mcp_ids": list(mcps or []),
            "skill_ids": list(skills or []),
            "kb_ids": list(enabled_kb_ids or []),
            "ontology_enabled": ontology_enabled,
            "ontology_runtime": ontology_runtime,
        }
    )
    payload = {
        "kind": "chat",
        "automation_task_id": task_id,
        "chat_mode": context["chat_mode"],
        "enable_thinking": context["enable_thinking"],
    }
    worker_args = {
        "session_messages": [{"role": "user", "content": prompt}],
        "effective_user_message": prompt, "raw_user_message": prompt,
        "context": context, "model_name": model,
    }
    accepted_run = await run_in_session(
        _accept_prompt_run,
        on_accept=_accept_link(
            automation_run_id, task_id, lease_token, {"kind": "chat", "worker_args": worker_args}
        ),
        chat_id=chat_id,
        user_id=user_id,
        user_content=prompt,
        request_payload=payload,
        model=model,
    )
    try:
        run = await admission.start_run(
            accepted_run=accepted_run,
            chat_id=chat_id,
            user_id=user_id,
            session_messages=[{"role": "user", "content": prompt}],
            effective_user_message=prompt,
            raw_user_message=prompt,
            context=context,
            request_payload=payload,
            model_name=model,
        )
    except BaseException as exc:
        await run_in_session(_abandon_run, accepted_run.run_id, str(exc) or type(exc).__name__)
        raise

    return await _wait(run, automation_run_id=automation_run_id, lease_token=lease_token)


async def execute_plan(
    *,
    user_id: str,
    task_name: str,
    plan_id: str,
    task_id: str,
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]],
    automation_run_id: Optional[str] = None,
    lease_token: Optional[str] = None,
):
    model = user_model_selection.resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS
    title = await run_in_session(_approve_plan, task_id, user_id, plan_id)
    content = f"自动化执行计划：{title}"
    chat_id, _ = await run_db(
        _prepare,
        user_id,
        task_id,
        task_name,
        content,
        model,
        automation_run_id,
        {"plan_chat": True, "plan_id": plan_id},
        lease_token=lease_token,
    )
    run = await _orchestration_plans_admission.start_plan_execute_run(
        on_accept=_accept_link(automation_run_id, task_id, lease_token),
        plan_id=plan_id,
        chat_id=chat_id,
        user_id=user_id,
        enabled_mcp_ids=enabled_mcp_ids,
        enabled_skill_ids=enabled_skill_ids,
        enabled_kb_ids=enabled_kb_ids,
        enabled_agent_ids=enabled_agent_ids,
        session_messages=[{"role": "user", "content": content}],
        model_name=model,
    )
    return await _wait(run, automation_run_id=automation_run_id, lease_token=lease_token)


async def execute_loop(
    *, user_id, task_name, loop_id, task_id, automation_run_id=None, lease_token=None
):
    if not loop_id:
        raise ValueError("loop 任务缺少 extra_data.loop_id")
    target = await run_in_session(_loop_target, user_id, task_name, loop_id, task_id)
    if isinstance(target, str):
        return None, target, {}
    chat_id, goal_spec, budget, project_context = target
    if await run_db(runtime.get_active_run_for_chat, chat_id, user_id):
        return chat_id, "loop 已在推进中，跳过本次触发", {}
    run = await _orchestration_loops_admission.start_autonomous_loop_run(
        on_accept=_accept_link(automation_run_id, task_id, lease_token),
        loop_id=loop_id,
        chat_id=chat_id,
        user_id=user_id,
        goal_spec=goal_spec,
        budget=budget,
        project_id=project_context.get("project_id"),
        automation_run=True,
    )
    return await _wait(run, automation_run_id=automation_run_id, lease_token=lease_token)

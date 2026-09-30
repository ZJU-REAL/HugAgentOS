"""Scheduled execution uses the same live run lifecycle as interactive conversations."""

import asyncio
import uuid
from typing import Dict, List, Optional, Tuple
from core.chat.context import build_runtime_context, resolve_enabled_capabilities
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db import engine as db_engine
from core.db.models import ChatMessage, ChatRun, ScheduledTaskRun
from core.services.chat_service import ChatService
from core.services.chat_sequencer import ChatSequencer
from core.services.automation_service import AutomationService
from core.services.automation_execution import task_execution_context
from core.services import user_model_selection
from orchestration import chat_run_executor as executor


class ConversationStopped(Exception):
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        super().__init__("用户已停止执行")


def _prepare(user_id, task_id, task_name, content, model_name, automation_run_id, metadata=None):
    chat_id = f"chat_{uuid.uuid4().hex[:16]}"
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
        _link_run(db, automation_run_id, task_id, chat_id)
    return chat_id, context


def _link_run(db, automation_run_id, task_id, chat_id):
    if automation_run_id is None:
        return
    run = db.get(ScheduledTaskRun, automation_run_id)
    if run is None or run.task_id != task_id:
        raise ValueError("定时任务执行记录不存在")
    run.chat_id = chat_id
    db.commit()


async def _wait(run) -> Tuple[str, str, Dict]:
    """Observe durable state without taking ownership of the executor's asyncio tasks.

    Cancellation of the scheduler (timeout/shutdown) must also fence the child run.
    User cancellation is an outcome, not cancellation of the scheduler itself.
    """
    try:
        while True:
            current = executor.get_run(run.run_id)
            if current is None:
                raise RuntimeError("会话执行记录不存在")
            if current.status not in ("pending", "running"):
                successor = executor.get_successor_run(run.run_id, user_id=run.user_id)
                if successor is not None:
                    run = successor
                    continue
                # A cancelled cooperative plan may still be releasing its writer.
                if current.status == "cancelled" and current.writer_slot:
                    await asyncio.sleep(0.1)
                    continue
                if current.status == "cancelled":
                    raise ConversationStopped(run.chat_id)
                if current.status != "completed":
                    raise RuntimeError(current.error_message or "会话执行未完成")
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
                    from core.llm._distill_shared import strip_think_blocks

                    result = strip_think_blocks(message.content or "") or "执行完成"
                    return run.chat_id, result, dict(message.usage or {})
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        # Fence first, then check the durable handoff. Completion may win
        # the cancellation CAS and atomically create a successor in between.
        while True:
            await executor.cancel_run(run.run_id, user_id=run.user_id)
            successor = executor.get_successor_run(run.run_id, user_id=run.user_id)
            if successor is None:
                break
            run = successor
        raise


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
):
    model = user_model_selection.resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS
    chat_id, project_context = _prepare(user_id, task_id, task_name, None, model, automation_run_id)
    with db_engine.SessionLocal() as db:
        skills, agents, mcps = resolve_enabled_capabilities(
            db, user_id, enabled_skill_ids, enabled_agent_ids, enabled_mcp_ids
        )
    from core.services.ontology_service import build_user_ontology_runtime

    ontology_enabled, ontology_runtime = build_user_ontology_runtime(user_id=user_id, task=prompt)
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
    with db_engine.SessionLocal() as db:
        with ChatSequencer(db).launching_main_run(
            chat_id=chat_id,
            user_id=user_id,
            user_content=prompt,
            request_payload=payload,
            model=model,
        ) as accepted:
            run = await executor.start_run(
                accepted_run=accepted.run,
                chat_id=chat_id,
                user_id=user_id,
                session_messages=[{"role": "user", "content": prompt}],
                effective_user_message=prompt,
                raw_user_message=prompt,
                context=context,
                request_payload=payload,
                model_name=model,
            )

    return await _wait(run)


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
):
    from core.services.plan_service import PlanService

    model = user_model_selection.resolve_effective_chat_model_name() or DEFAULT_CHAT_MODEL_ALIAS
    with db_engine.SessionLocal() as db:
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
    content = f"自动化执行计划：{title}"
    chat_id, _ = _prepare(
        user_id,
        task_id,
        task_name,
        content,
        model,
        automation_run_id,
        {"plan_chat": True, "plan_id": plan_id},
    )
    run = await executor.start_plan_execute_run(
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
    return await _wait(run)


async def execute_loop(*, user_id, task_name, loop_id, task_id, automation_run_id=None):
    from core.services.loop_service import LoopService

    if not loop_id:
        raise ValueError("loop 任务缺少 extra_data.loop_id")
    with db_engine.SessionLocal() as db:
        task = AutomationService(db).get_task(task_id, user_id)
        if task is None:
            raise ValueError("任务不存在或无权访问")
        project_context = task_execution_context(db, task)
        loop = LoopService(db).get_loop(loop_id)
        if loop is None or loop.user_id != user_id:
            raise ValueError("loop 不存在或无权访问")
        if loop.status in ("completed", "cancelled"):
            return None, f"loop 已终态({loop.status})，跳过", {}
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
        _link_run(db, automation_run_id, task_id, chat_id)
    if executor.get_active_run_for_chat(chat_id, user_id):
        return chat_id, "loop 已在推进中，跳过本次触发", {}
    run = await executor.start_autonomous_loop_run(
        loop_id=loop_id,
        chat_id=chat_id,
        user_id=user_id,
        goal_spec=goal_spec,
        budget=budget,
        project_id=project_context.get("project_id"),
        automation_run=True,
    )
    return await _wait(run)

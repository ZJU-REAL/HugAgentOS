"""Admit durable autonomous Loop runs."""

from __future__ import annotations

from typing import Any, Dict, Optional

import core.runs.ownership as ownership
from core.db.models import ChatRun
from core.infra.logging import get_logger
from core.db.threads import run_in_session

logger = get_logger(__name__)


async def start_autonomous_loop_run(
    *,
    loop_id: str,
    chat_id: str,
    user_id: str,
    goal_spec: Dict[str, Any],
    budget: Dict[str, Any],
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    evaluator_model: Optional[str] = None,
    worker_max_iters: int = 15,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    is_resume: bool = False,
    project_id: Optional[str] = None,
    automation_run: bool = False,
    on_accept=None,
) -> ChatRun:
    """Start an autonomous-loop run (background task + Redis Stream, mirroring start_plan_execute_run)."""
    if not chat_id:
        raise ValueError("chat_id is required to start an autonomous loop run")
    request_payload = {
        "kind": "autonomous_loop",
        "loop_id": loop_id,
        "chat_id": chat_id,
        "goal_spec": goal_spec,
        "budget": budget,
        "model_name": model_name,
        "model_provider_id": model_provider_id,
        "evaluator_model": evaluator_model,
        "worker_max_iters": worker_max_iters,
        "hitl_enabled": hitl_enabled,
        "enable_thinking": enable_thinking,
        # Thinking level (fast/medium/high/max): the active-run probe endpoint
        # uses this to restore the frontend's resume phase; the worker uses it
        # to set reasoning_effort.
        "chat_mode": chat_mode,
        "is_resume": is_resume,
        "project_id": project_id,
        "automation_run": automation_run,
    }
    # 启动参数跟 loop 持久化（而非只活在本次请求里）：崩溃/重启后的续跑——无论 API
    # resume 还是启动自动续跑——都能还原同一套模型/评审模型/轮数/思考档位，不再悄悄
    # 降级到默认值。
    try:
        from features.automation.loops import LoopService as _LoopSvc

        def _save_start_params(db):
            _LoopSvc(db).save_start_params(
                loop_id,
                {
                    "model_name": model_name,
                    "model_provider_id": model_provider_id,
                    "evaluator_model": evaluator_model,
                    "worker_max_iters": worker_max_iters,
                    "hitl_enabled": hitl_enabled,
                    "enable_thinking": enable_thinking,
                    "chat_mode": chat_mode,
                    "automation_run": automation_run,
                },
            )
        await run_in_session(_save_start_params)
    except Exception:  # noqa: BLE001 - 参数存档失败不阻塞启动
        logger.warning("loop start_params persist failed", exc_info=True)
    initial_args = {
        "loop_id": loop_id,
        "goal_spec": goal_spec,
        "budget": budget,
        "model_name": model_name,
        "model_provider_id": model_provider_id,
        "evaluator_model": evaluator_model,
        "worker_max_iters": worker_max_iters,
        "hitl_enabled": hitl_enabled,
        "enable_thinking": enable_thinking,
        "chat_mode": chat_mode,
        "is_resume": is_resume,
        "project_id": project_id,
        "automation_run": automation_run,
    }
    run = ownership.create_run_record(
        on_accept=on_accept,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot={
            "kind": "autonomous_loop",
            "initial_args": initial_args,
            "worker_args": {
                "context": {
                    "model_name": model_name,
                    "model_provider_id": model_provider_id,
                    "chat_mode": chat_mode,
                    "project_ctx": {"project_id": project_id} if project_id else None,
                    "automation_run": automation_run,
                }
            },
        },
    )
    from orchestration.pending import dispatch_pending
    await dispatch_pending(run)
    logger.info(
        "autonomous_loop_run_started",
        run_id=run.run_id,
        loop_id=loop_id,
        user_id=user_id,
    )
    return run

"""Resume eligible autonomous Loops after process restart."""

from __future__ import annotations

import os

import orchestration.loops.admission as _orchestration_loops_admission
from core.infra.logging import get_logger
from core.db.threads import run_in_session

logger = get_logger(__name__)


def _mark_interrupted(db, loop_ids) -> None:
    from features.automation.loops import LoopService

    for loop_id in loop_ids:
        LoopService(db).mark_interrupted(
            loop_id, reason="服务重启导致运行中断，可点击「继续」从断点续跑"
        )


async def resume_running_loops() -> int:
    """启动对账 + 按需自动续跑：进程重启后收养/归位孤儿自主循环。

    进程刚起来时不存在任何活跃 task，因此 status='running' 的 loop 全是孤儿。
    对每一个孤儿：

    - ``LOOP_AUTO_RESUME=true`` → 用**持久化的启动参数**（agent_loops.extra_data.
      start_params：模型/评审模型/轮数/思考档位）原样续跑——账本在 DB 有镜像、
      沙箱有 feature_list.json，driver 自动断点续跑，不再悄悄降级到默认模型。
    - 关闭（默认）→ 状态归位为 ``interrupted``（可续跑），不再留下永远 running 的
      僵尸行（历史坑：僵尸 running 行既误导列表页、又让 cancel 无处下手）。

    'awaiting_human'/终态一律不动。
    """
    from core.db.models import AgentLoop

    auto = os.getenv("LOOP_AUTO_RESUME", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    resumed = 0
    def _running_loop_specs(db):
        loops = db.query(AgentLoop).filter(AgentLoop.status == "running").all()
        specs = [
            (
                x.loop_id,
                x.chat_id,
                x.user_id,
                dict(x.goal_spec or {}),
                dict(x.budget or {}),
                (x.extra_data or {}).get("project_id"),
                dict((x.extra_data or {}).get("start_params") or {}),
            )
            for x in loops
        ]
        return specs
    specs = await run_in_session(_running_loop_specs)

    if not auto:
        if specs:
            await run_in_session(_mark_interrupted, [spec[0] for spec in specs])
            logger.info("[startup] orphan loops marked interrupted: %d", len(specs))
        return 0

    for loop_id, chat_id, user_id, goal_spec, budget, project_id, params in specs:
        if not chat_id:
            continue
        try:
            await _orchestration_loops_admission.start_autonomous_loop_run(
                loop_id=loop_id,
                chat_id=chat_id,
                user_id=user_id,
                goal_spec=goal_spec,
                budget=budget,
                model_name=params.get("model_name"),
                model_provider_id=params.get("model_provider_id"),
                evaluator_model=params.get("evaluator_model"),
                worker_max_iters=int(params.get("worker_max_iters") or 15),
                hitl_enabled=bool(params.get("hitl_enabled")),
                enable_thinking=bool(params.get("enable_thinking")),
                chat_mode=params.get("chat_mode"),
                project_id=project_id,
                automation_run=bool(params.get("automation_run")),
                is_resume=True,
            )
            resumed += 1
            logger.info("autonomous_loop_resumed", loop_id=loop_id)
        except Exception:  # noqa: BLE001
            logger.warning("autonomous_loop_resume_failed loop_id=%s", loop_id, exc_info=True)
    return resumed

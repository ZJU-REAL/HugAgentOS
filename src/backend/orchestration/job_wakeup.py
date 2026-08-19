"""作业唤醒 —— 后台作业跑到里程碑/终点时，把智能体叫回同一个会话继续干活。

为什么需要它：``run_job(wait=False)`` 让主对话立刻脱身（小时级作业不能让一次工具调用
挂那么久），但作业跑完之后**没人叫醒智能体**——用户得再说一句话才会回来看结果。
这就是"后台干完活，前台不知道"。

做法与定时任务同源：由驱动在**同一个会话**里入队一条新的 chat run，带一条系统口吻的
指令。智能体因此像收到一条新消息一样自己醒过来。前端不用改——它本来就会看到会话里多
出一轮对话。

两种唤醒，别混：

- **终态唤醒** ``wake_on_job_finish``：作业进终态叫一次，让智能体读台账、写交付。
  靠 ``jobs.metadata.woken_at`` 打标去重（驱动重入、进程重启续跑都不会重复叫）。
- **中途唤醒** ``wake_on_job_progress``：作业跑几十分钟甚至几小时时，只在终点播报等于
  全程失联——用户既不知道还剩多少，也不知道是不是早就卡死了。所以按间隔播报一次进度，
  **只汇报、不干活**（提示词里明令禁止重复提交作业/导出台账）。会话里已有在跑的 run
  时直接跳过：那说明智能体正忙，再入队只会堆栈。

注意中途唤醒是"贵"的（每次都是一轮真实推理），所以间隔默认 5 分钟、且要求确有进展或
确已停滞才叫——纯粹的噪声播报不如不叫。实时进度看输入框上方的作业状态条（零推理成本），
中途唤醒解决的是"智能体自己该不该介入"。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified

from core.chat.plan_progress import (
    load_plan_progress,
    plan_is_unfinished,
    render_plan_checklist,
)
from core.db.engine import SessionLocal
from core.db.models import Job

logger = logging.getLogger(__name__)


def _wake_prompt(job: Job, stats: Dict[str, Any], plan: Optional[Dict[str, Any]] = None) -> str:
    """写给智能体自己的续跑指令 —— 只给事实与下一步，不替它下结论。"""
    remaining = int(stats.get("remaining", 0))
    lines = [
        f"[系统] 你先前提交的批量作业「{job.name or job.job_id}」已结束。",
        f"作业 ID：{job.job_id}　状态：{job.status}",
        (
            f"台账统计：总计 {stats.get('total', 0)} 项，已完成 {stats.get('done', 0)}，"
            f"查无 {stats.get('not_found', 0)}，失败 {stats.get('failed', 0)}，"
            f"待办 {stats.get('pending', 0)}。"
        ),
    ]
    if job.error_message:
        lines.append(f"作业错误信息：{job.error_message}")
    lines.append("")
    # 计划清单收尾 —— 排在交付**之前**，而不是末尾一句"顺手也可以做"。
    #
    # 线上实测（2026-08-18）：软性提醒放在结尾时，交付轮一次都没调 update_plan，计划栏
    # 就停在 2/5。所以这里把清单**原样念给它**（状态逐条列出），让要做的动作退化成
    # "把这几行的 status 改一改再传回来"。
    if plan_is_unfinished(plan):
        lines.append(
            "⚠️ 本轮**第一件事**：调用 `update_plan` 把任务计划清单收尾。"
            "作业丢后台之后没有任何一轮动过它，它现在停在：\n"
            f"{render_plan_checklist(plan)}\n"
            "传入全量步骤列表：这次真做完的标 `completed`；没做/做不成的保持原状并在回复里"
            "说明——**不要**把没做的写成 completed。收完尾再做下面的交付。\n"
        )
    if remaining > 0:
        lines.append(
            f"仍有 {remaining} 项未结算。请先判断是环境故障还是脚本问题：可以用 "
            f"run_job(action='resume', job_id='{job.job_id}') 断点续跑（已完成的项不会重做），"
            "也可以改脚本后再 resume。**不要**把未完成当作完成交付。"
        )
    else:
        lines.append(
            "全部工作项已结算。请读取作业结果完成最终交付：产出用户要的文件，"
            "并如实报告覆盖率（分母、完成数、查无数）与未覆盖清单。"
        )
    lines.append("查看明细：run_job(action='status', job_id='%s')。" % job.job_id)
    return "\n".join(lines)


def _progress_prompt(job: Job, stats: Dict[str, Any], budget_left: Dict[str, Any], stalled: bool) -> str:
    """进度播报 —— 只让智能体转述现状，**不要**让它顺手再干点什么。

    这里的措辞是刻意的：中途唤醒的每一次都是一轮真实推理，如果不把边界写死，智能体
    很容易"顺手"再提交一个作业或把台账明细读进对话，既烧钱又污染上下文。
    """
    total = int(stats.get("total", 0))
    settled = int(stats.get("settled", 0))
    pct = int(settled * 100 / total) if total else 0
    lines = [
        f"[系统] 进度播报：你提交的批量作业「{job.name or job.job_id}」仍在后台运行。",
        f"作业 ID：{job.job_id}",
        (
            f"台账：共 {total} 项，已结算 {settled}（{pct}%）——完成 {stats.get('done', 0)}，"
            f"查无 {stats.get('not_found', 0)}，失败 {stats.get('failed', 0)}，"
            f"处理中 {stats.get('running', 0)}，待办 {stats.get('pending', 0)}。"
        ),
        (
            f"预算余量：调用 {budget_left.get('calls_left', 0)}，"
            f"墙钟 {int(budget_left.get('seconds_left', 0) / 60)} 分钟。"
        ),
        "",
    ]
    if stalled:
        lines.append(
            "⚠️ 距上次播报**没有任何新的结算**。请判断是不是卡住了："
            f"可以 run_job(action='status', job_id='{job.job_id}') 看明细，"
            "确认是环境故障还是脚本缺陷；确实跑不动就 cancel 掉改脚本，别干等。"
        )
    else:
        lines.append(
            "作业推进正常。**请只用一两句话把上面的进度转述给用户**，然后结束本轮回复。"
        )
    lines.append(
        "本轮的硬性边界：不要重复提交作业（它还在跑），不要 export 台账，"
        "不要把逐项结果读进对话，也不用动 `update_plan` 计划清单"
        "（作业仍停在同一步，收尾留到作业跑完那一轮）——作业跑完会再叫你一次，那时才做交付。"
    )
    return "\n".join(lines)


async def wake_on_job_progress(
    job_id: str, *, stats: Dict[str, Any], budget_left: Dict[str, Any], stalled: bool
) -> bool:
    """作业运行途中播报一次进度。返回是否真的发起了唤醒。

    三道闸：作业得还在跑、得是 ``wait=False`` 提交的、会话里不能已有在跑的 run。
    最后一道最重要——智能体正忙时再入队一轮，只会让两轮互相打架。
    """
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if job is None or not job.chat_id:
            return False
        if job.status != "running":
            return False
        meta = dict(job.extra_data or {})
        if not dict(meta.get("start_params") or {}).get("wake_on_finish"):
            return False  # wait=True 的调用方本来就阻塞在那儿等，不需要播报
        chat_id = job.chat_id
        user_id = job.user_id
        model_name = dict(meta.get("start_params") or {}).get("model_name")
        prompt = _progress_prompt(job, stats, budget_left, stalled)

    try:
        from orchestration.chat_run_executor import get_active_run_for_chat

        if get_active_run_for_chat(chat_id, user_id) is not None:
            logger.info("[job-wake] skip progress wake, chat busy chat=%s job=%s", chat_id, job_id)
            return False
    except Exception:  # noqa: BLE001 —— 探测失败就当它不忙，宁可多叫一次也别哑掉
        pass

    try:
        await _enqueue_followup_run(
            chat_id=chat_id,
            user_id=user_id,
            message=prompt,
            model_name=model_name,
            wake_kind="job_progress",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[job-wake] progress enqueue failed job=%s: %s", job_id, exc)
        return False
    logger.info("[job-wake] progress woke chat=%s job=%s stalled=%s", chat_id, job_id, stalled)
    return True


async def wake_on_job_finish(job_id: str) -> bool:
    """作业终态后叫醒会话。返回是否真的发起了唤醒。"""
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if job is None or not job.chat_id:
            return False
        if job.status not in ("completed", "failed", "cancelled"):
            return False
        meta = dict(job.extra_data or {})
        if meta.get("woken_at"):
            return False  # 已经叫过了
        start_params = dict(meta.get("start_params") or {})
        if not start_params.get("wake_on_finish"):
            return False  # 只有 wait=False 提交的作业才需要唤醒
        chat_id = job.chat_id
        user_id = job.user_id
        model_name = start_params.get("model_name")

        from core.services.job_service import JobService

        stats = JobService(db).stats(job_id)
        prompt = _wake_prompt(job, stats, load_plan_progress(chat_id, db))

        meta["woken_at"] = True
        job.extra_data = meta
        flag_modified(job, "extra_data")
        db.commit()

    try:
        await _enqueue_followup_run(
            chat_id=chat_id,
            user_id=user_id,
            message=prompt,
            model_name=model_name,
            wake_kind="job_finish",
        )
    except Exception as exc:  # noqa: BLE001 —— 唤醒失败不该反过来影响作业本身的终态
        logger.warning("[job-wake] enqueue failed job=%s: %s", job_id, exc)
        return False
    logger.info("[job-wake] woke chat=%s for job=%s", chat_id, job_id)
    return True


async def _enqueue_followup_run(
    *,
    chat_id: str,
    user_id: str,
    message: str,
    model_name: Optional[str],
    wake_kind: str,
) -> None:
    """在既有会话里入队一条 chat run —— 与用户手动发一条消息走的是同一条路。

    这样前端不用任何改动：它本来就会在会话里看到新的一轮（断线还能按 run_id 续播）。
    """
    from api.routes.v1.chats import _load_session_messages
    from core.chat.context import build_runtime_context
    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.services.chat_service import ChatService
    from orchestration import chat_run_executor

    with SessionLocal() as db:
        chat_service = ChatService(db)
        session_messages = _load_session_messages(chat_service, chat_id, user_id)
        skills, agents, mcps = resolve_all_runtime_enabled(db, user_id)
        # 会话模式要跟着唤醒轮一起走。作业只可能诞生在工作流模式里，而 workflow_mode
        # 决定了 run_job 注不注册、批量提示词注不注入——唤醒轮丢掉它，模型就会看到
        # 一条"用 run_job(action='resume') 续跑"的系统消息，却在工具面里找不到
        # run_job，只能弃用作业、把 N 个工作项拖回主循环逐项重做（成本二次方增长，
        # 前端也再没有后台作业卡片）。会话元数据里的 workflow_chat 是这件事的真源，
        # 用户消息路径读的也是它（见 chats.py 的 ctx 组装）。
        _sess = chat_service.get_session(chat_id, user_id)
        workflow_chat = bool(dict(getattr(_sess, "extra_data", None) or {}).get("workflow_chat"))
        # 唤醒消息按 user 角色落库：它要能进历史、能被模型看见，走和普通消息完全一样的链路。
        # 但它是**写给模型的系统指令**，不是用户说的话——`hidden_in_chat` 让消息列表接口
        # 把它从聊天记录里滤掉（模型侧的历史另走 load_session_history，不受影响）。
        # 没有这个标记，用户一刷新页面就会看到「[系统] 进度播报：……请只用一两句话转述」
        # 这种内部提示词以自己的口吻贴在对话里。
        chat_service.add_message(
            chat_id=chat_id,
            role="user",
            content=message,
            extra_data={"system_wake": wake_kind, "hidden_in_chat": True},
        )

    session_messages.append({"role": "user", "content": message})
    context = build_runtime_context(
        model_name=model_name,
        user_id=user_id,
        chat_id=chat_id,
        enabled_skills=skills,
        enabled_agents=agents,
        enabled_mcps=mcps,
    )
    context["workflow_chat"] = workflow_chat
    await chat_run_executor.start_run(
        chat_id=chat_id,
        user_id=user_id,
        session_messages=session_messages,
        effective_user_message=message,
        raw_user_message=message,
        context=context,
        request_payload={"chat_id": chat_id, "message": message, "kind": "job_wakeup"},
        model_name=model_name,
    )

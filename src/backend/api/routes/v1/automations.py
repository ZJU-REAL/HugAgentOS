"""Automation API routes — CRUD for scheduled tasks + notifications."""

import json
from typing import Any, Dict, List, Optional

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.infra.responses import success_response, created_response
from core.services.automation_service import AutomationService
from core.infra.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/automations", tags=["Automations"])


# ── Request Schemas ────────────────────────────────────────────

class CreateAutomationRequest(BaseModel):
    task_type: str = Field(..., pattern=r"^(prompt|plan|loop)$")
    prompt: Optional[str] = Field(None, max_length=5000)
    plan_id: Optional[str] = None
    loop_id: Optional[str] = None  # task_type=loop: the autonomous loop advanced on schedule
    cron_expression: str = Field(..., min_length=9, max_length=100)
    schedule_type: Optional[str] = Field(None, pattern=r"^(recurring|once|manual)$")
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    enabled_mcp_ids: Optional[List[str]] = None
    enabled_skill_ids: Optional[List[str]] = None
    enabled_kb_ids: Optional[List[str]] = None
    enabled_agent_ids: Optional[List[str]] = None
    max_runs: Optional[int] = None
    # Optional: deliver the result on schedule to an external IM channel conversation (Lark, etc.)
    channel_id: Optional[str] = None
    conversation_id: Optional[str] = None


class UpdateAutomationRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    cron_expression: Optional[str] = Field(None, min_length=9, max_length=100)
    schedule_type: Optional[str] = Field(None, pattern=r"^(recurring|once|manual)$")
    prompt: Optional[str] = Field(None, max_length=5000)
    enabled_mcp_ids: Optional[List[str]] = None
    enabled_skill_ids: Optional[List[str]] = None
    enabled_kb_ids: Optional[List[str]] = None
    enabled_agent_ids: Optional[List[str]] = None
    # Change delivery target: passing channel_id+conversation_id rebinds the channel conversation;
    # explicitly passing null switches back to in-site only.
    # Use model_fields_set to distinguish "not passed (untouched)" from "passed null (cleared)".
    channel_id: Optional[str] = None
    conversation_id: Optional[str] = None


class NotificationIdsRequest(BaseModel):
    ids: List[str]


# ── Endpoints ──────────────────────────────────────────────────

@router.post("", summary="创建自动化任务")
async def create_automation(
    req: CreateAutomationRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建一个定时自动化任务。支持 prompt / plan 两种任务类型，需提供合法的 cron 表达式；plan 类型还需校验 plan_id 归属。"""
    # Validate cron expression
    if not croniter.is_valid(req.cron_expression):
        raise HTTPException(status_code=400, detail="无效的 cron 表达式")

    # Validate task content
    if req.task_type == "prompt" and not req.prompt:
        raise HTTPException(status_code=400, detail="提示词类型任务必须提供 prompt")
    if req.task_type == "plan":
        if not req.plan_id:
            raise HTTPException(status_code=400, detail="计划类型任务必须提供 plan_id")
        from core.services.plan_service import PlanService
        plan_svc = PlanService(db)
        plan = plan_svc.get_plan(req.plan_id, user.user_id)
        if not plan:
            raise HTTPException(status_code=404, detail="计划不存在或无权访问")

    if req.task_type == "loop":
        if not req.loop_id:
            raise HTTPException(status_code=400, detail="循环类型任务必须提供 loop_id")
        from core.services.loop_service import LoopService
        if not LoopService(db).get_loop(req.loop_id, user_id=user.user_id):
            raise HTTPException(status_code=404, detail="循环不存在或无权访问")

    schedule_type = req.schedule_type or "recurring"

    # Channel delivery destination (optional): must be a bot owned by the user, to prevent delivering to someone else's bot
    task_metadata: Optional[dict] = None
    if req.task_type == "loop":
        task_metadata = {"loop_id": req.loop_id}
    if req.channel_id and req.conversation_id:
        from core.db.repository.channel import ChannelConnectionRepository
        conn = ChannelConnectionRepository(db).get_by_id(req.channel_id)
        if conn is None or conn.owner_user_id != str(user.user_id):
            raise HTTPException(status_code=403, detail="无权投递到该渠道机器人")
        task_metadata = {**(task_metadata or {}),
                         "channel_id": req.channel_id, "conversation_id": req.conversation_id}

    svc = AutomationService(db)
    task = svc.create_task(
        user_id=user.user_id,
        task_type=req.task_type,
        prompt=req.prompt,
        plan_id=req.plan_id,
        cron_expression=req.cron_expression,
        schedule_type=schedule_type,
        name=req.name,
        description=req.description or "",
        timezone=req.timezone,
        enabled_mcp_ids=req.enabled_mcp_ids,
        enabled_skill_ids=req.enabled_skill_ids,
        enabled_kb_ids=req.enabled_kb_ids,
        enabled_agent_ids=req.enabled_agent_ids,
        max_runs=req.max_runs,
        metadata=task_metadata,
    )
    return created_response(data=AutomationService.task_to_dict(task))


@router.get("", summary="自动化任务列表")
async def list_automations(
    status: Optional[str] = None,
    sidebar_activated: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户的全部自动化任务，可按状态、侧边栏激活状态过滤并分页。"""
    svc = AutomationService(db)
    tasks = svc.list_tasks(
        user.user_id,
        status_filter=status,
        sidebar_activated=sidebar_activated,
        limit=limit,
        offset=offset,
    )
    return success_response(data=[AutomationService.task_to_dict(t) for t in tasks])


@router.get("/{task_id}", summary="自动化任务详情")
async def get_automation(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定自动化任务的详细信息，任务不存在返回 404。"""
    svc = AutomationService(db)
    task = svc.get_task(task_id, user.user_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return success_response(data=AutomationService.task_to_dict(task))


@router.patch("/{task_id}", summary="更新自动化任务")
async def update_automation(
    task_id: str,
    req: UpdateAutomationRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新自动化任务的名称、描述、cron 表达式、任务内容等字段；若改 cron 会校验合法性，无更新字段返回 400。"""
    if req.cron_expression and not croniter.is_valid(req.cron_expression):
        raise HTTPException(status_code=400, detail="无效的 cron 表达式")

    updates = req.model_dump(exclude_none=True)
    # The delivery target lives in extra_data (not a task column), handled separately: covers "rebind channel" and "switch back to in-site".
    deliver_touched = bool({"channel_id", "conversation_id"} & req.model_fields_set)
    updates.pop("channel_id", None)
    updates.pop("conversation_id", None)
    if not updates and not deliver_touched:
        raise HTTPException(status_code=400, detail="无更新字段")

    svc = AutomationService(db)
    if deliver_touched:
        task0 = svc.get_task(task_id, user.user_id)
        if not task0:
            raise HTTPException(status_code=404, detail="任务不存在")
        new_ed = dict(task0.extra_data or {})
        # First clear old delivery markers (flat ones + channel targets inside the list)
        new_ed.pop("channel_id", None)
        new_ed.pop("conversation_id", None)
        if isinstance(new_ed.get("delivery_targets"), list):
            new_ed["delivery_targets"] = [
                t for t in new_ed["delivery_targets"]
                if not (isinstance(t, dict) and t.get("type") == "channel")
            ]
            if not new_ed["delivery_targets"]:
                new_ed.pop("delivery_targets")
        # If a channel conversation is given -> verify bot ownership then write; otherwise keep it cleared (= in-site only)
        if req.channel_id and req.conversation_id:
            from core.db.repository.channel import ChannelConnectionRepository
            conn = ChannelConnectionRepository(db).get_by_id(req.channel_id)
            if conn is None or conn.owner_user_id != str(user.user_id):
                raise HTTPException(status_code=403, detail="无权投递到该渠道机器人")
            new_ed["channel_id"] = req.channel_id
            new_ed["conversation_id"] = req.conversation_id
        updates["extra_data"] = new_ed

    task = svc.update_task(task_id, user.user_id, **updates)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return success_response(data=AutomationService.task_to_dict(task))


@router.delete("/{task_id}", summary="删除自动化任务")
async def delete_automation(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除指定自动化任务及其运行历史记录。"""
    svc = AutomationService(db)
    deleted = svc.delete_task(task_id, user.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return success_response(message="已删除")


@router.post("/{task_id}/pause", summary="暂停自动化任务")
async def pause_automation(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """暂停一个处于活跃状态的自动化任务，任务不存在或不可暂停时返回 400。"""
    svc = AutomationService(db)
    task = svc.pause_task(task_id, user.user_id)
    if not task:
        raise HTTPException(status_code=400, detail="任务不存在或不可暂停")
    return success_response(data=AutomationService.task_to_dict(task))


@router.post("/{task_id}/resume", summary="恢复自动化任务")
async def resume_automation(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """恢复一个已暂停的自动化任务，任务不存在或不可恢复时返回 400。"""
    svc = AutomationService(db)
    task = svc.resume_task(task_id, user.user_id)
    if not task:
        raise HTTPException(status_code=400, detail="任务不存在或不可恢复")
    return success_response(data=AutomationService.task_to_dict(task))


@router.post("/{task_id}/trigger", summary="手动触发自动化任务")
async def trigger_automation(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """立即手动触发一次自动化任务执行（异步），仅 active / paused 状态可触发。"""
    svc = AutomationService(db)
    task = svc.get_task(task_id, user.user_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail=f"任务状态为 '{task.status}'，无法手动触发")

    from orchestration.schedulers.automation_scheduler import get_scheduler
    scheduler = get_scheduler()
    if scheduler:
        import asyncio
        asyncio.create_task(scheduler.execute_task(task.task_id, task.user_id))
    return success_response(message="已触发执行")


@router.post("/{task_id}/activate-sidebar", summary="侧边栏激活任务")
async def activate_sidebar(
    task_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """将自动化任务标记为侧边栏已激活（幂等操作），任务不存在返回 404。"""
    svc = AutomationService(db)
    task = svc.activate_sidebar(task_id, user.user_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return success_response(data=AutomationService.task_to_dict(task))


@router.get("/{task_id}/runs", summary="自动化任务运行历史")
async def get_automation_runs(
    task_id: str,
    limit: int = 10,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定自动化任务的运行历史记录，可通过 limit 限制返回条数。"""
    svc = AutomationService(db)
    runs = svc.get_task_runs(task_id, user.user_id, limit=limit)
    return success_response(data=[AutomationService.run_to_dict(r) for r in runs])


# ── Notifications (Redis-backed) ──────────────────────────────

_NOTIF_TTL = 7 * 24 * 3600


async def _modify_notification_list(user_id: str, ids: set, transform):
    """Read notification list from Redis, apply *transform* to each matched
    item, and rewrite the list.  *transform(item)* returns the modified item
    dict to keep, or ``None`` to drop it."""
    from core.infra.redis import get_redis
    redis = get_redis()
    if not redis:
        return
    key = f"jx:notifications:{user_id}"
    raw_items = await redis.lrange(key, 0, -1)
    kept = []
    for raw in raw_items:
        try:
            item = json.loads(raw)
            if item.get("id") in ids:
                item = transform(item)
                if item is None:
                    continue
            kept.append(json.dumps(item, ensure_ascii=False))
        except (json.JSONDecodeError, TypeError):
            kept.append(raw if isinstance(raw, str) else raw.decode())
    async with redis.pipeline(transaction=True) as pipe:
        await pipe.delete(key)
        if kept:
            await pipe.rpush(key, *kept)
        await pipe.expire(key, _NOTIF_TTL)
        await pipe.execute()


@router.get("/notifications/list", summary="获取自动化通知列表")
async def get_notifications(
    user: UserContext = Depends(get_current_user),
):
    """获取当前用户的自动化任务通知列表（基于 Redis，最多返回最近 50 条）。"""
    try:
        from core.infra.redis import get_redis
        redis = get_redis()
        if not redis:
            return success_response(data=[])
        key = f"jx:notifications:{user.user_id}"
        raw_items = await redis.lrange(key, 0, 49)
        notifications = []
        for raw in raw_items:
            try:
                notifications.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
        return success_response(data=notifications)
    except Exception as exc:
        logger.warning("Failed to fetch notifications: %s", exc)
        return success_response(data=[])


@router.post("/notifications/read", summary="标记通知为已读")
async def mark_notifications_read(
    req: NotificationIdsRequest,
    user: UserContext = Depends(get_current_user),
):
    """按 ID 列表将指定的自动化通知标记为已读。"""
    try:
        def _mark_read(item):
            item["read"] = True
            return item
        await _modify_notification_list(user.user_id, set(req.ids), _mark_read)
        return success_response(message="ok")
    except Exception as exc:
        logger.warning("Failed to mark notifications read: %s", exc)
        return success_response(message="ok")


@router.post("/notifications/delete", summary="删除通知")
async def delete_notifications(
    req: NotificationIdsRequest,
    user: UserContext = Depends(get_current_user),
):
    """按 ID 列表删除指定的自动化通知。"""
    try:
        await _modify_notification_list(user.user_id, set(req.ids), lambda _: None)
        return success_response(message="ok")
    except Exception as exc:
        logger.warning("Failed to delete notifications: %s", exc)
        return success_response(message="ok")

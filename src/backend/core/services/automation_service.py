"""Automation service — CRUD for scheduled tasks and run history."""

from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import uuid

from croniter import croniter
import pytz
from sqlalchemy.orm import Session

from core.db.models import ScheduledTask, ScheduledTaskRun
from core.infra.logging import get_logger

logger = get_logger(__name__)

# ── 运行结果摘要截断策略（单一真源）────────────────────────────────────
# ScheduledTaskRun.result_summary 存的是**全文**：它同时是渠道（钉钉/飞书等）投递的正文，
# 在写库/执行侧截断会让定时消息只发出开头一段。所以截断一律发生在展示侧，且统一走
# truncate_summary，保证各处「被截断」的观感一致（都带省略号）。
SUMMARY_LIMIT_LIST = 500  # 运行历史列表：前端只做两行 line-clamp 展示
SUMMARY_LIMIT_BRIEF = 200  # 通知中心卡片 / MCP brief：进模型上下文或小卡片，要更短


def truncate_summary(text: Optional[str], limit: Optional[int]) -> Optional[str]:
    """按 limit 截断展示用摘要；``limit`` 为 None/0 表示不截断，原样返回。"""
    if not limit or not text or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _channel_target_fields(extra_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Parse the first channel delivery target from a task's extra_data, filling in
    {channel_id, conversation_id}.

    Handles both the delivery_targets list and the old flat form (resolve_delivery_targets
    unifies them). No channel target → both are None (= site-internal only). Exposed via
    task_to_dict to backfill the frontend edit form.
    """
    try:
        from core.services.delivery_targets import resolve_delivery_targets

        tgt = next(
            (t for t in resolve_delivery_targets(extra_data or {}) if t.get("type") == "channel"),
            None,
        )
    except Exception:  # noqa: BLE001
        tgt = None
    return {
        "channel_id": tgt.get("channel_id") if tgt else None,
        "conversation_id": tgt.get("conversation_id") if tgt else None,
    }


def compute_next_run(
    cron_expression: str,
    timezone: str = "Asia/Shanghai",
    base_time: Optional[datetime] = None,
) -> datetime:
    """Compute the next fire time from a cron expression in the given timezone.

    Returns a timezone-aware UTC datetime.
    """
    tz = pytz.timezone(timezone)
    base = base_time or datetime.now(tz)
    if base.tzinfo is None:
        base = tz.localize(base)
    cron = croniter(cron_expression, base)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(pytz.utc)


class AutomationService:
    """Service for scheduled-task operations."""

    def __init__(self, db: Session):
        self.db = db

    # ── Task CRUD ──────────────────────────────────────────────────

    def create_task(
        self,
        *,
        user_id: str,
        task_type: str,
        prompt: Optional[str] = None,
        plan_id: Optional[str] = None,
        cron_expression: str,
        schedule_type: str = "recurring",
        name: Optional[str] = None,
        description: str = "",
        timezone: str = "Asia/Shanghai",
        enabled_mcp_ids: Optional[List[str]] = None,
        enabled_skill_ids: Optional[List[str]] = None,
        enabled_kb_ids: Optional[List[str]] = None,
        enabled_agent_ids: Optional[List[str]] = None,
        max_runs: Optional[int] = None,
        metadata: Optional[dict] = None,
        commit: bool = True,
    ) -> ScheduledTask:
        task_id = f"auto_{uuid.uuid4().hex[:16]}"

        # schedule_type is the sole authority; the recurring column is derived from it (only "recurring" is a periodic task).
        recurring = schedule_type == "recurring"

        # Manual tasks are never auto-scheduled; next_run_at stays NULL so scheduler skips them.
        if schedule_type == "manual":
            next_run = None
        else:
            next_run = compute_next_run(cron_expression, timezone)

        if not recurring:
            max_runs = 1

        task = ScheduledTask(
            task_id=task_id,
            user_id=user_id,
            task_type=task_type,
            prompt=prompt,
            plan_id=plan_id,
            cron_expression=cron_expression,
            recurring=recurring,
            schedule_type=schedule_type,
            timezone=timezone,
            enabled_mcp_ids=enabled_mcp_ids or [],
            enabled_skill_ids=enabled_skill_ids or [],
            enabled_kb_ids=enabled_kb_ids or [],
            enabled_agent_ids=enabled_agent_ids or [],
            status="active",
            next_run_at=next_run,
            max_runs=max_runs,
            name=name,
            description=description,
            extra_data=metadata or {},
        )
        self.db.add(task)
        if commit:
            self.db.commit()
            self.db.refresh(task)
        else:
            self.db.flush()
        return task

    def get_task(self, task_id: str, user_id: str) -> Optional[ScheduledTask]:
        task = self.db.query(ScheduledTask).filter(ScheduledTask.task_id == task_id).first()
        if task and task.user_id != user_id:
            return None
        return task

    def get_task_by_id(self, task_id: str) -> Optional[ScheduledTask]:
        """Get task without ownership check (for scheduler)."""
        return self.db.query(ScheduledTask).filter(ScheduledTask.task_id == task_id).first()

    def list_tasks(
        self,
        user_id: str,
        status_filter: Optional[str] = None,
        sidebar_activated: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ScheduledTask]:
        q = self.db.query(ScheduledTask).filter(ScheduledTask.user_id == user_id)
        if status_filter:
            q = q.filter(ScheduledTask.status == status_filter)
        if sidebar_activated is not None:
            q = q.filter(ScheduledTask.sidebar_activated == sidebar_activated)
        return q.order_by(ScheduledTask.created_at.desc()).offset(offset).limit(limit).all()

    def activate_sidebar(self, task_id: str, user_id: str) -> Optional[ScheduledTask]:
        """Mark a task as sidebar-activated (idempotent)."""
        task = self.get_task(task_id, user_id)
        if not task:
            return None
        if not task.sidebar_activated:
            task.sidebar_activated = True
            task.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(task)
        return task

    def update_task(
        self,
        task_id: str,
        user_id: str,
        *,
        commit: bool = True,
        **kwargs: Any,
    ) -> Optional[ScheduledTask]:
        task = self.get_task(task_id, user_id)
        if not task:
            return None
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        # If schedule_type changed, re-derive the recurring column to keep the two consistent (read by the scheduler).
        if "schedule_type" in kwargs:
            task.recurring = task.schedule_type == "recurring"
        # Manual tasks never fire automatically — clear next_run_at regardless of cron.
        # For recurring/once, recompute if cron or schedule_type changed.
        if task.schedule_type == "manual":
            task.next_run_at = None
        elif "cron_expression" in kwargs or "schedule_type" in kwargs:
            task.next_run_at = compute_next_run(task.cron_expression, task.timezone)
        task.updated_at = datetime.now(timezone.utc)
        if commit:
            self.db.commit()
            self.db.refresh(task)
        else:
            self.db.flush()
        return task

    def update_task_system(self, task_id: str, **kwargs: Any) -> Optional[ScheduledTask]:
        """Update task fields without ownership check (for scheduler)."""
        task = self.get_task_by_id(task_id)
        if not task:
            return None
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(task)
        return task

    def delete_task(self, task_id: str, user_id: str, *, commit: bool = True) -> bool:
        task = (
            self.db.query(ScheduledTask)
            .filter(
                ScheduledTask.task_id == task_id,
                ScheduledTask.user_id == user_id,
            )
            .first()
        )
        if not task:
            return False
        self.db.delete(task)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return True

    def pause_task(self, task_id: str, user_id: str) -> Optional[ScheduledTask]:
        task = self.get_task(task_id, user_id)
        if not task or task.status != "active":
            return None
        task.status = "paused"
        task.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(task)
        return task

    def resume_task(self, task_id: str, user_id: str) -> Optional[ScheduledTask]:
        task = self.get_task(task_id, user_id)
        if not task or task.status != "paused":
            return None
        task.status = "active"
        task.next_run_at = compute_next_run(task.cron_expression, task.timezone)
        task.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(task)
        return task

    # ── Due tasks (for scheduler) ──────────────────────────────────

    def get_due_tasks(self, now: datetime) -> List[ScheduledTask]:
        # Manual-type tasks are never auto-fired; they can only be triggered via /trigger.
        return (
            self.db.query(ScheduledTask)
            .filter(
                ScheduledTask.status == "active",
                ScheduledTask.schedule_type != "manual",
                ScheduledTask.next_run_at <= now,
            )
            .all()
        )

    @staticmethod
    def _is_oneshot_done(task: ScheduledTask) -> bool:
        """True when no more runs are scheduled after the current one
        (one-shot, non-recurring, or max_runs reached). Manual tasks excluded —
        they stay active and are only ever fired via /trigger."""
        if task.schedule_type == "manual":
            return False
        return (
            (task.schedule_type == "once")
            or (not task.recurring)
            or bool(task.max_runs and (task.run_count or 0) >= task.max_runs)
        )

    def advance_next_run(self, task_id: str) -> None:
        task = self.get_task_by_id(task_id)
        if not task:
            return
        task.run_count = (task.run_count or 0) + 1

        # Manual tasks: stay active, no next_run_at, run_count just grows.
        if task.schedule_type == "manual":
            task.next_run_at = None
        # One-shot / max_runs reached: clear next_run_at so the schedule stops
        # re-firing. Do NOT flip status to "completed" here — this runs as a
        # PRE-advance (before execute_task), and execute_task's guard refuses any
        # task whose status isn't active/paused. Marking it completed now would
        # make the executor skip the very run it just fired (never records a run,
        # never delivers to the channel). Terminal status is set AFTER the run by
        # finalize_after_run().
        elif self._is_oneshot_done(task):
            task.next_run_at = None
        else:
            task.next_run_at = compute_next_run(task.cron_expression, task.timezone)

        task.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def bump_run_count(self, task_id: str) -> None:
        """手动触发路径专用的计数递增。

        run_count 原本只在 advance_next_run 里 +1，而那是**调度器轮询**的前置步骤。
        手动「立即执行」和 schedule_type='manual' 的任务根本不走调度器（get_due_tasks
        把 manual 排除在外），于是这些执行只落了一条 run 记录、累计次数却纹丝不动——
        详情页出现「有 5 条执行记录、累计执行 0 次」的自相矛盾。

        这里不动调度器那条路径的语义（那边 +1 与 next_run_at 推进必须原子发生），
        只补上手动触发这一路的计数。
        """
        task = self.get_task_by_id(task_id)
        if not task:
            return
        task.run_count = (task.run_count or 0) + 1
        task.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def finalize_after_run(self, task_id: str) -> None:
        """Mark one-shot / exhausted tasks 'completed' once their run has
        finished. Recurring tasks stay active (advance_next_run already moved
        their next_run_at forward). Never overrides an auto-disabled task."""
        task = self.get_task_by_id(task_id)
        if not task or task.status == "disabled":
            return
        if self._is_oneshot_done(task):
            task.status = "completed"
            task.next_run_at = None
            task.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    # ── Run history ───────────────────────────────────────────────

    def record_run_start(self, task_id: str) -> ScheduledTaskRun:
        run = ScheduledTaskRun(
            run_id=f"run_{uuid.uuid4().hex[:16]}",
            task_id=task_id,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def record_run_complete(
        self,
        run_id: str,
        *,
        status: str,
        chat_id: Optional[str] = None,
        result_summary: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: int = 0,
        usage: Optional[Dict] = None,
    ) -> Optional[ScheduledTaskRun]:
        run = self.db.query(ScheduledTaskRun).filter(ScheduledTaskRun.run_id == run_id).first()
        if not run:
            return None
        run.status = status
        run.chat_id = chat_id
        run.result_summary = result_summary
        run.error_message = error_message
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms
        run.usage = usage or {}
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_task_runs(self, task_id: str, user_id: str, limit: int = 10) -> List[ScheduledTaskRun]:
        task = self.get_task(task_id, user_id)
        if not task:
            return []
        return (
            self.db.query(ScheduledTaskRun)
            .filter(ScheduledTaskRun.task_id == task_id)
            .order_by(ScheduledTaskRun.started_at.desc())
            .limit(limit)
            .all()
        )

    # ── Serialization ─────────────────────────────────────────────

    @staticmethod
    def task_to_dict(task: ScheduledTask) -> Dict[str, Any]:
        plan_title = None
        if task.plan_id and task.plan:
            plan_title = task.plan.title
        return {
            "task_id": task.task_id,
            "user_id": task.user_id,
            "task_type": task.task_type,
            "prompt": task.prompt,
            "plan_id": task.plan_id,
            "plan_title": plan_title,
            "cron_expression": task.cron_expression,
            "schedule_type": task.schedule_type or "recurring",
            "timezone": task.timezone,
            "enabled_mcp_ids": task.enabled_mcp_ids or [],
            "enabled_skill_ids": task.enabled_skill_ids or [],
            "enabled_kb_ids": task.enabled_kb_ids or [],
            "enabled_agent_ids": task.enabled_agent_ids or [],
            "status": task.status,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
            "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
            "run_count": task.run_count or 0,
            "max_runs": task.max_runs,
            "consecutive_failures": task.consecutive_failures or 0,
            "max_failures": task.max_failures or 3,
            "last_error": task.last_error,
            "name": task.name,
            "description": task.description,
            "sidebar_activated": bool(task.sidebar_activated),
            # Channel delivery target (for frontend edit backfill): parse the first channel
            # target from delivery_targets / the old flat channel_id. None if absent (= site-internal only).
            **_channel_target_fields(task.extra_data),
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }

    @staticmethod
    def run_to_dict(
        run: ScheduledTaskRun, *, summary_limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """运行记录 → dict。

        ``result_summary`` 存的是**全文**（渠道投递要用完整正文，见 automation_scheduler），
        因此只展示摘要的消费方应传 ``summary_limit`` 自行截断，避免把一整篇报告塞进列表
        响应/模型上下文。不传 = 原样返回全文（供需要完整结果的详情场景使用）。
        """
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status,
            "chat_id": run.chat_id,
            "result_summary": truncate_summary(run.result_summary, summary_limit),
            "error_message": run.error_message,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration_ms": run.duration_ms,
            "usage": run.usage or {},
        }

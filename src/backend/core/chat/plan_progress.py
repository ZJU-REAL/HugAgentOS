"""计划栏状态落库 —— 让"任务计划"有一份跨轮次、跨刷新、跨标签页都成立的真源。

为什么需要这一份：``update_plan`` 的进度过去只活在**当前这条 SSE 流**里。同一轮对话里
这没问题，但工作流模式把活丢给后台作业之后，一份计划要跨好几轮才走完：

    第 1 轮：列出 5 步 → 提交作业 → 本轮结束（计划停在 2/5）
    …作业在后台跑几十分钟，期间只有进度播报轮…
    最后一轮：作业跑完，智能体读台账、写交付 → 这时才该把清单收尾

中间任何一次刷新、切标签页、或者用户压根没在看这个会话，前端那份内存态就没了；而"最后
一轮该收尾"这件事，过去只写在提示词里，模型不照做（线上实测：日志里 update_plan 停在
2/5，交付轮一次都没再调）计划栏就永远停在半路。

落库之后有三个用处：
1. **唤醒轮能把清单原样念给模型**（见 ``orchestration/job_wakeup``）——让它做的是"把这份
   清单更新一下"这种机械动作，而不是凭记忆重建；
2. **刷新后能把计划栏还原**（会话 metadata 顺带发给前端），长作业期间刷新不再丢；
3. **收尾不依赖前端**：一轮跑完就在服务端记 settled，前端就算全程没跟这条流，下次拿到
   会话数据也知道这份计划已经结束、不该再转圈。

存在会话的 ``extra_data['plan_progress']`` 里（不是消息上）：计划栏是"当前这段任务"的
状态，天然属于会话，且列表接口本来就把 metadata 发给前端，不用另开接口。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from core.db.engine import SessionLocal
from core.db.models import ChatSession

logger = logging.getLogger(__name__)

PLAN_KEY = "plan_progress"

_VALID_STATUSES = ("pending", "in_progress", "completed", "failed")


def _normalize_steps(raw: Any) -> List[Dict[str, str]]:
    steps: List[Dict[str, str]] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        if not title:
            continue
        status = str(s.get("status") or "pending").strip().lower()
        if status not in _VALID_STATUSES:
            status = "pending"
        steps.append({"title": title, "status": status})
    return steps


def _write(chat_id: str, mutate) -> None:
    """在会话 extra_data 上做一次小改写。计划栏的落库失败绝不能影响正在跑的那一轮。"""
    if not chat_id:
        return
    try:
        with SessionLocal() as db:
            session = db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
            if session is None:
                return
            meta = dict(session.extra_data or {})
            if mutate(meta) is False:
                return
            session.extra_data = meta
            flag_modified(session, "extra_data")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plan-progress] persist failed chat=%s: %s", chat_id, exc)


def save_plan_progress(chat_id: str, plan: Dict[str, Any], *, settled: bool = False) -> None:
    """记下模型刚报的这份清单（全量替换，与 update_plan 的语义一致）。"""
    steps = _normalize_steps(plan.get("steps"))
    if not steps:
        return

    def _mutate(meta: Dict[str, Any]) -> None:
        meta[PLAN_KEY] = {
            "title": str(plan.get("title") or ""),
            "steps": steps,
            "settled": bool(settled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    _write(chat_id, _mutate)


def settle_plan_progress(chat_id: str) -> None:
    """把当前这份清单标成"已收尾"——步骤状态一个字都不改。

    收尾只表示"没有哪一轮还会来更新它了"，不表示"每一步都做完了"。停在 2/5 的计划
    settle 之后依然显示 2/5，只是不再转圈——**不能**在这里把剩下的步骤伪造成完成。
    """

    def _mutate(meta: Dict[str, Any]):
        plan = meta.get(PLAN_KEY)
        if not isinstance(plan, dict) or plan.get("settled"):
            return False  # 没有计划、或早就收过尾 → 不写库
        plan["settled"] = True
        plan["updated_at"] = datetime.now(timezone.utc).isoformat()
        meta[PLAN_KEY] = plan
        return None

    _write(chat_id, _mutate)


def clear_plan_progress(chat_id: str) -> None:
    """用户发了新一轮消息 —— 上一段任务的计划栏就此作废（与前端发送时的清空对齐）。"""

    def _mutate(meta: Dict[str, Any]):
        if PLAN_KEY not in meta:
            return False
        meta.pop(PLAN_KEY, None)
        return None

    _write(chat_id, _mutate)


def load_plan_progress(chat_id: str, db=None) -> Optional[Dict[str, Any]]:
    """读回当前会话的计划清单；没有则 None。``db`` 可复用调用方已开的会话。"""
    if not chat_id:
        return None

    def _read(session_db) -> Optional[Dict[str, Any]]:
        row = session_db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
        meta = dict(getattr(row, "extra_data", None) or {}) if row else {}
        plan = meta.get(PLAN_KEY)
        return dict(plan) if isinstance(plan, dict) else None

    try:
        if db is not None:
            return _read(db)
        with SessionLocal() as own:
            return _read(own)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plan-progress] load failed chat=%s: %s", chat_id, exc)
        return None


def render_plan_checklist(plan: Dict[str, Any]) -> str:
    """把清单渲染成给模型看的几行文本（唤醒提示词里用）。"""
    label = {
        "completed": "completed",
        "in_progress": "in_progress",
        "failed": "failed",
        "pending": "pending",
    }
    lines = []
    for i, s in enumerate(plan.get("steps") or [], start=1):
        lines.append(f"  {i}) {s.get('title', '')} —— {label.get(s.get('status'), 'pending')}")
    return "\n".join(lines)


def plan_is_unfinished(plan: Optional[Dict[str, Any]]) -> bool:
    """还有步骤没结算（pending / in_progress）——这才需要交付轮去收尾。"""
    if not isinstance(plan, dict):
        return False
    steps = plan.get("steps") or []
    return bool(steps) and any(
        s.get("status") in ("pending", "in_progress") for s in steps if isinstance(s, dict)
    )

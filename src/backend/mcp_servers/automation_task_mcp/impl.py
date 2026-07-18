"""定时任务管理 MCP —— 业务实现（直连 DB，按 X-Current-User-Id 归属）。

复用后端 ``AutomationService`` 的全套 CRUD；投递目标经 ``delivery_targets`` 模型泛化。
所有操作强制按 ``user_id`` 归属，绝不跨用户。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _svc(db):
    from core.services.automation_service import AutomationService
    return AutomationService(db)


def _valid_cron(expr: str) -> bool:
    try:
        from croniter import croniter
        return croniter.is_valid(expr)
    except Exception:
        return False


def _resolve_ref(db, user_id: str, ref: str) -> Tuple[Optional[Any], List[Any]]:
    """task_ref → (唯一命中 task, 候选列表)。先精确 task_id，再按名称模糊匹配。

    返回 (task, candidates)：命中唯一 → (task, [])；多命中 → (None, candidates)；无 → (None, [])。
    """
    from core.db.models import ScheduledTask

    ref = (ref or "").strip()
    if not ref:
        return None, []
    exact = (
        db.query(ScheduledTask)
        .filter(ScheduledTask.task_id == ref, ScheduledTask.user_id == user_id)
        .first()
    )
    if exact:
        return exact, []
    cands = (
        db.query(ScheduledTask)
        .filter(ScheduledTask.user_id == user_id, ScheduledTask.name.ilike(f"%{ref}%"))
        .order_by(ScheduledTask.created_at.desc())
        .limit(10)
        .all()
    )
    if len(cands) == 1:
        return cands[0], []
    return None, cands


def _task_brief(task) -> Dict[str, Any]:
    from core.services.automation_service import AutomationService
    from core.services.delivery_targets import resolve_delivery_targets, describe_targets

    d = AutomationService.task_to_dict(task)
    targets = resolve_delivery_targets(task.extra_data)
    return {
        "task_id": d.get("task_id"),
        "name": d.get("name"),
        "cron_expression": d.get("cron_expression"),
        "status": d.get("status"),
        "next_run_at": d.get("next_run_at"),
        "last_run_at": d.get("last_run_at"),
        "run_count": d.get("run_count"),
        "prompt": (task.prompt or "")[:200],
        "delivery": describe_targets(targets),
        "delivery_targets": targets,
    }


def _candidates_payload(cands: List[Any]) -> Dict[str, Any]:
    return {
        "ok": False,
        "need_clarification": True,
        "message": "匹配到多个任务，请用 task_id 指明具体哪一个：",
        "candidates": [
            {"task_id": c.task_id, "name": c.name, "cron_expression": c.cron_expression,
             "status": c.status}
            for c in cands
        ],
    }


# ── 渠道会话列举（供跨会话投递选目标）────────────────────────────────────
def list_conversations(*, user_id: str) -> Dict[str, Any]:
    """列出本人渠道 bot 产生过的会话（群/私聊），供跨会话投递时按名称定位目标。"""
    if not user_id:
        return {"ok": False, "message": "❌ 无法确定用户身份。"}
    from core.db.engine import SessionLocal
    from core.services.channel_service import list_owner_conversations

    with SessionLocal() as db:
        convs = list_owner_conversations(db, user_id, limit=100)
    msg = (f"共 {len(convs)} 个渠道会话。" if convs
           else "暂无渠道会话（需先有人给你的机器人发过消息才会产生会话）。")
    return {"ok": True, "count": len(convs), "conversations": convs, "message": msg}


def _resolve_create_targets(
    user_id: str, deliver_to: Optional[str], channel_origin: Optional[Dict[str, Any]], db,
):
    """把 deliver_to 解析成 delivery_targets（复用调用方的 db）。返回 (targets, error_message)。

    deliver_to：空 → 按来源自动；"inapp" → 仅站内；否则当作 conversation_id（须本人拥有的渠道会话）。
    """
    from core.services.delivery_targets import build_delivery_targets

    val = (deliver_to or "").strip()
    if not val:
        return build_delivery_targets(channel_origin=channel_origin), None
    if val == "inapp":
        return [{"type": "inapp"}], None
    # 指定某个渠道会话：按 conversation_id 反查归属本人的 channel_id
    from core.db.models import ChatSession

    sess = (
        db.query(ChatSession)
        .filter(
            ChatSession.user_id == user_id,
            ChatSession.external_conversation_id == val,
            ChatSession.channel_id.isnot(None),
        )
        .first()
    )
    if sess is None:
        return None, (f"❌ 没找到会话「{val}」。先用 list_channel_conversations 获取可投递的会话，"
                      f"再用其中的 conversation_id。")
    return [
        {"type": "inapp"},
        {"type": "channel", "channel_id": sess.channel_id, "conversation_id": val},
    ], None


# ── 工具实现 ────────────────────────────────────────────────────────────
def create_task(
    *, user_id: str, cron_expression: str, prompt: str,
    name: str = "", deliver_to: Optional[str] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not user_id:
        return {"ok": False, "message": "❌ 无法确定用户身份。"}
    if not (prompt or "").strip():
        return {"ok": False, "message": "❌ prompt 不能为空。"}
    if not _valid_cron(cron_expression):
        return {"ok": False, "message": f"❌ cron 表达式无效：{cron_expression}"}

    from core.db.engine import SessionLocal
    from core.services.delivery_targets import describe_targets

    with SessionLocal() as db:
        # 投递目标：deliver_to 不填 → 按来源自动（渠道会话→站内+该会话；网页→站内）；
        # "inapp" → 仅站内；conversation_id → 指定的某个渠道会话。
        targets, err = _resolve_create_targets(user_id, deliver_to, channel_origin, db)
        if err:
            return {"ok": False, "message": err}
        task = _svc(db).create_task(
            user_id=user_id, task_type="prompt", prompt=prompt,
            cron_expression=cron_expression, schedule_type="recurring",
            name=(name or "定时任务")[:200],
            metadata={"delivery_targets": targets},
        )
        brief = _task_brief(task)
    return {
        "ok": True,
        "message": f"✅ 已创建定时任务「{brief['name']}」（{cron_expression}），投递到：{describe_targets(targets)}。",
        "task": brief,
    }


def list_tasks(*, user_id: str, status: str = "active") -> Dict[str, Any]:
    if not user_id:
        return {"ok": False, "message": "❌ 无法确定用户身份。"}
    from core.db.engine import SessionLocal

    status_filter = None if status in ("", "all", None) else status
    with SessionLocal() as db:
        rows = _svc(db).list_tasks(user_id, status_filter=status_filter, limit=50)
        tasks = [_task_brief(t) for t in rows]
    return {"ok": True, "count": len(tasks), "tasks": tasks,
            "message": f"共 {len(tasks)} 个定时任务。" if tasks else "你当前没有定时任务。"}


def get_task(*, user_id: str, task_ref: str) -> Dict[str, Any]:
    from core.db.engine import SessionLocal
    from core.services.automation_service import AutomationService

    with SessionLocal() as db:
        task, cands = _resolve_ref(db, user_id, task_ref)
        if cands:
            return _candidates_payload(cands)
        if not task:
            return {"ok": False, "message": f"❌ 没找到任务：{task_ref}"}
        brief = _task_brief(task)
        runs = _svc(db).get_task_runs(task.task_id, limit=5)
        brief["recent_runs"] = [AutomationService.run_to_dict(r) for r in runs]
    return {"ok": True, "task": brief}


def update_task(
    *, user_id: str, task_ref: str,
    cron_expression: Optional[str] = None, prompt: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    if cron_expression and not _valid_cron(cron_expression):
        return {"ok": False, "message": f"❌ cron 表达式无效：{cron_expression}"}
    from core.db.engine import SessionLocal

    patch: Dict[str, Any] = {}
    if cron_expression:
        patch["cron_expression"] = cron_expression
    if prompt is not None and prompt.strip():
        patch["prompt"] = prompt
    if name is not None and name.strip():
        patch["name"] = name.strip()[:200]
    if not patch:
        return {"ok": False, "message": "❌ 未提供任何要修改的字段。"}

    with SessionLocal() as db:
        task, cands = _resolve_ref(db, user_id, task_ref)
        if cands:
            return _candidates_payload(cands)
        if not task:
            return {"ok": False, "message": f"❌ 没找到任务：{task_ref}"}
        updated = _svc(db).update_task(task.task_id, user_id, **patch)
        brief = _task_brief(updated)
    return {"ok": True, "message": f"✅ 已更新「{brief['name']}」。", "task": brief}


def _toggle(user_id: str, task_ref: str, action: str) -> Dict[str, Any]:
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        task, cands = _resolve_ref(db, user_id, task_ref)
        if cands:
            return _candidates_payload(cands)
        if not task:
            return {"ok": False, "message": f"❌ 没找到任务：{task_ref}"}
        svc = _svc(db)
        if action == "pause":
            r = svc.pause_task(task.task_id, user_id)
            verb, st = "暂停", "paused"
        elif action == "resume":
            r = svc.resume_task(task.task_id, user_id)
            verb, st = "恢复", "active"
        else:  # delete
            ok = svc.delete_task(task.task_id, user_id)
            return {"ok": bool(ok),
                    "message": f"✅ 已删除「{task.name}」。" if ok else "❌ 删除失败。"}
        if r is None:
            return {"ok": False, "message": f"❌ 任务当前状态无法{verb}（需为对应前置状态）。"}
        return {"ok": True, "message": f"✅ 已{verb}「{r.name}」。", "task": _task_brief(r)}


def pause_task(*, user_id: str, task_ref: str) -> Dict[str, Any]:
    return _toggle(user_id, task_ref, "pause")


def resume_task(*, user_id: str, task_ref: str) -> Dict[str, Any]:
    return _toggle(user_id, task_ref, "resume")


def delete_task(*, user_id: str, task_ref: str) -> Dict[str, Any]:
    return _toggle(user_id, task_ref, "delete")

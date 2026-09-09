"""Automation component execution across a desktop's two authorized backends."""
from __future__ import annotations
import asyncio
import json

TOOLS = {
    "create_scheduled_task": "create_task", "list_scheduled_tasks": "list_tasks",
    "get_scheduled_task": "get_task", "update_scheduled_task": "update_task",
    "pause_scheduled_task": "pause_task", "resume_scheduled_task": "resume_task",
    "delete_scheduled_task": "delete_task",
}


def creation_arguments(arguments, user_id, chat_id):
    from core.db.engine import SessionLocal
    from core.db.models import ChatSession, Project
    from core.auth.desktop_bridge import bridge_enabled
    from core.services.automation_execution import instance_location

    args = dict(arguments)
    with SessionLocal() as db:
        chat = db.get(ChatSession, chat_id) if chat_id else None
        if not args.get("project_id") and chat and chat.user_id == user_id and chat.deleted_at is None:
            if chat.project_id:
                args["project_id"] = chat.project_id
        project = db.get(Project, args["project_id"]) if args.get("project_id") else None
        if not args.get("execution_location"):
            args["execution_location"] = (
                "local" if project and project.kind == "local"
                else "cloud" if bridge_enabled() else instance_location()
            )
    return args


async def local_automation_result(source_plugin, tool_name, arguments, headers, *, authorize=None):
    """Return None for cloud execution; local dispatch requires the automation component."""
    if source_plugin != "automation" or tool_name not in TOOLS:
        return None
    from core.services import desktop_cloud_bridge as bridge
    from core.db.engine import SessionLocal
    from core.db.models import ChatRun, ScheduledTask, ChatSession
    from core.services.tool_effect_ledger import CURRENT_TOOL_EFFECT
    from core.services.automation_execution import instance_location

    if instance_location() != "local":
        return None
    bridge.ensure_current_authorization()
    args = dict(arguments)
    # User identity belongs to the durable local run, not model arguments.
    effect = CURRENT_TOOL_EFFECT.get()
    with SessionLocal() as db:
        run = db.get(ChatRun, effect.run_id) if effect else None
        chat_id = next((v for k, v in headers.items() if k.lower() == "x-chat-id"), "")
        chat = db.get(ChatSession, chat_id) if chat_id else None
        user_id = run.user_id if run else chat.user_id if chat else None
        if not user_id:
            raise ValueError("无法确定本机任务的当前用户")
        from core.services.desktop_capability_protocol import token_subject
        if token_subject((bridge.get_state() or {}).get("token", "")) != user_id:
            raise ValueError("本机任务所属账号与当前云端账号不匹配")
        location = args.get("execution_location") or ""
        if location not in {"", "local", "cloud"}:
            raise ValueError("执行位置必须为本机或云端")
        if tool_name == "create_scheduled_task":
            args = creation_arguments(args, user_id, run.chat_id if run else chat_id)
            location = args["execution_location"]
            if location == "cloud" and args.get("project_id"):
                from core.db.models import Project
                project = db.get(Project, args["project_id"])
                if project is not None and project.kind == "local":
                    raise ValueError("本机项目不能在云端执行，请选择本机")
        elif not location and args.get("task_ref"):
            local = db.get(ScheduledTask, args["task_ref"])
            if local and local.user_id == user_id:
                location = "local"
            elif db.query(ScheduledTask).filter(
                ScheduledTask.user_id == user_id,
                ScheduledTask.name.ilike(f"%{args['task_ref']}%"),
            ).first() is not None:
                raise ValueError("本机存在同名任务，请明确 execution_location 或使用任务 ID")
        merge_cloud = tool_name == "list_scheduled_tasks" and not location
        if merge_cloud:
            location = "local"
        if location != "local":
            return None
    if authorize is None:
        raise ValueError("本机工具缺少当前授权校验")
    await authorize()
    bridge.ensure_current_authorization()
    if tool_name != "create_scheduled_task":
        args.pop("execution_location", None)
    from mcp_servers.automation_task_mcp import impl
    result = await asyncio.to_thread(getattr(impl, TOOLS[tool_name]), user_id=user_id, **args)
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool._response import ToolChunk
    return ToolChunk(content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
                     state=ToolResultState.SUCCESS if result.get("ok") else ToolResultState.ERROR,
                     metadata={"origin": "local", "execution_location": "local", "merge_cloud": merge_cloud})

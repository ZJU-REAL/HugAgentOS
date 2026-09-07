"""Canonical instruction reads for project chats; writes only during initialization."""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from core.llm.tools._common import resp_json


def register_project_instruction_tools(toolkit, *, project_id: str, user_id: str, local_path=None, allow_write=True):
    from core.llm.tool_permissions import READ, WRITE, local_path_tool
    from core.llm.tools._myspace_confirm import OP_EDIT

    target = f"{local_path.rstrip('/')}/AGENTS.md" if local_path else "/myspace/AGENTS.md"

    def operate(content=None, revision=None):
        from core.auth.permissions_iface import resolve_project_permission
        from core.db.engine import SessionLocal
        from core.db.models import Project
        from core.services.project_instructions import ProjectInstructionsService

        if content is None:
            from core.services.project_instructions import read_authorized_project_instructions
            return {"ok": True, "path": "AGENTS.md", **read_authorized_project_instructions(project_id, user_id)}

        with SessionLocal() as db:
            project = db.query(Project).filter(
                Project.project_id == project_id, Project.deleted_at.is_(None)
            ).first()
            if project is None or resolve_project_permission(db, user_id, project) not in ("admin", "edit"):
                raise HTTPException(403, "项目不存在或已失去编辑权限")
            service = ProjectInstructionsService(db)
            if content is not None:
                if not revision:
                    raise HTTPException(409, "先调用 read_project_instructions 获取最新 revision")
                service.write(project, user_id, content, expected_revision=revision)
                db.commit()
            return {"ok": True, "path": "AGENTS.md", **service.read(project)}

    async def read_project_instructions():
        """读取当前项目根 AGENTS.md 的最新持久内容及 instructions_revision。
        项目规则以本工具返回的最新持久内容为准；历史消息和沙箱副本可能过期。
        初始化前、保存前和保存后均用本工具核验。
        """
        try:
            if local_path:
                from core.llm.tool_permissions import require_local_path_permission
                require_local_path_permission(target, "read")
            return resp_json(await asyncio.to_thread(operate))
        except HTTPException as exc:
            return resp_json({"ok": False, "error": exc.detail, "status": exc.status_code})

    async def save_project_instructions(content: str, expected_revision: str):
        """将完整 Markdown 持久保存到当前项目根 AGENTS.md 并读回。
        仅能修改当前项目的这一个文件，本地、个人、组织项目使用同一入口。
        Args:
            content: 完整的新指令，UTF-8 不超过 32 KiB。
            expected_revision: 最近一次 read_project_instructions 返回的 instructions_revision。
        版本冲突时必须重新读取并合并，不能盲目覆盖。失败不能宣称已同步。
        """
        try:
            if local_path:
                from core.llm.tool_permissions import require_local_path_permission
                require_local_path_permission(target, "write")
            return resp_json(await asyncio.to_thread(operate, content, expected_revision))
        except HTTPException as exc:
            return resp_json({"ok": False, "error": exc.detail, "status": exc.status_code})

    toolkit.register_tool_function(
        read_project_instructions,
        permission=local_path_tool("file_path", READ, tool_name="read_project_instructions", default_path=target),
    )
    if not allow_write:
        return
    toolkit.register_tool_function(
        save_project_instructions,
        permission=local_path_tool(
            "file_path", WRITE, tool_name="save_project_instructions",
            default_path=target, myspace_op=OP_EDIT,
        ),
    )

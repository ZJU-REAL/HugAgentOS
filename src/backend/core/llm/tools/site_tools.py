"""Read-only site discovery for any chat in a local project."""

from __future__ import annotations

import asyncio
from fastapi import HTTPException
from core.llm.tools._common import resp_json


def register_project_site_tools(toolkit, *, project_id: str, user_id: str):
    async def list_project_sites():
        """查询当前本地项目、当前云端账号关联的已发布站点，不修改或绑定会话。
        返回所有候选的 site_id、title、url、version、source_dir、publish_dir。
        从项目入口或新会话编辑也先查询；按用户目标选择，多项无法区分时询问。
        查询失败不能视为没有站点。核对目录中的入口文件后，编辑发布显式传
        site_id 和 src_dir=publish_dir；构建型另传 source_dir 并先重新构建。
        """
        from core.services.local_site_sources import project_sources

        try:
            items = await asyncio.to_thread(project_sources, user_id, project_id)
            return resp_json({"ok": True, "project_id": project_id, "items": items})
        except HTTPException as exc:
            return resp_json({"ok": False, "error": exc.detail, "status": exc.status_code})

    toolkit.register_tool_function(list_project_sites)

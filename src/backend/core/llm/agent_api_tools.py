"""Native tools for an API agent: private account data is never mounted."""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import posixpath
import tempfile
from pathlib import Path

from core.llm.agent_api_runtime import AgentApiExecutionScope, artifact_in_scope
from core.llm.tools._tool_helpers import _resp_json


def _provider():
    from core.sandbox import get_sandbox_provider
    provider = get_sandbox_provider()
    # A workspace directory in a shared process is not a filesystem boundary.
    # Host/local and script-runner providers cannot isolate an external key
    # from other account workspaces. OpenSandbox uses a separate container.
    if provider.name != "opensandbox" or provider.runs_on_host:
        raise ValueError("API 沙箱操作需要独立容器隔离，当前沙箱不支持此模式")
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        raise ValueError("当前环境未启用沙箱操作")
    return provider


def _workspace_path(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("沙箱路径不能为空")
    path = posixpath.normpath(value if value.startswith("/") else "/workspace/" + value)
    if not path.startswith("/workspace/") or path.startswith("/workspace/myspace/"):
        raise ValueError("只允许访问当前 API 沙箱的工作目录")
    return path


def register_agent_api_tools(toolkit, scope: AgentApiExecutionScope, *, read_only=False, skill_dirs=None):
    """Register a fixed scoped surface; do not wrap account-private tools."""
    from core.sandbox import ProcessRequest, SandboxError
    from core.llm.agent_api_skills import ApiSkillStager
    skills = ApiSkillStager(skill_dirs)

    async def bash(command: str, timeout: int | None = None, yield_time_ms: int = 60000):
        """在当前 API 会话的独立沙箱中执行命令，不挂载个人目录或登录凭据。"""
        try:
            if not command.strip() or (timeout is not None and timeout <= 0):
                raise ValueError("命令不能为空，timeout 必须为正数")
            provider = _provider()
            await skills.stage(provider, scope)
            return _resp_json(await provider.start_process(
                ProcessRequest(
                    script_content=command, script_name="_api.sh", language="bash",
                    timeout=timeout, user_id=scope.sandbox_user_id,
                    session_id=scope.sandbox_session_id,
                ),
                yield_time_ms=max(0, min(yield_time_ms, 60000)),
            ))
        except (ValueError, OSError, SandboxError) as exc:
            return _resp_json({"error": str(exc)})

    async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 60000):
        """读取或继续当前 API 沙箱启动的进程。"""
        try:
            return _resp_json(await _provider().write_stdin(
                session_id, sandbox_session_id=scope.sandbox_session_id,
                user_id=scope.sandbox_user_id, chars=chars,
                yield_time_ms=max(0, min(yield_time_ms, 60000)),
            ))
        except (ValueError, OSError, SandboxError) as exc:
            return _resp_json({"error": str(exc)})

    async def read_artifact(
        file_id: str, offset: int = 0, limit: int = 4000,
        sheet_name: str | None = None, slide_index: int | None = None,
    ):
        """读取当前 API 会话中生成的文件，不能读取账号其他文件。"""
        if not await asyncio.to_thread(artifact_in_scope, file_id, scope):
            return _resp_json({"error": "文件不属于当前 API 会话或已删除"})
        from core.llm.tool_collector import ToolCollector
        from core.llm.tools.read_artifact_tool import register_read_artifact
        collector = ToolCollector()
        register_read_artifact(collector, user_id=scope.owner_user_id)
        return await collector.get_tool("read_artifact")._func(
            file_id=file_id, offset=offset, limit=limit,
            sheet_name=sheet_name, slide_index=slide_index,
        )

    async def pin_to_workspace(file_ids: list[str]):
        """交付当前 API 会话已生成的文件；只接受文件 ID，不能交付主机路径。"""
        if not isinstance(file_ids, list) or not file_ids:
            return _resp_json({"error": "请提供当前会话的文件 ID 列表"})
        for file_id in file_ids:
            if not isinstance(file_id, str) or not await asyncio.to_thread(artifact_in_scope, file_id, scope):
                return _resp_json({"error": "文件不属于当前 API 会话或已删除"})
        from core.artifacts.store import get_artifact
        from core.db.engine import SessionLocal
        from core.db.models import Artifact
        from core.llm import workspace
        from core.services.artifact_service import persist_artifacts

        def persist():
            refs = []
            for file_id in file_ids:
                item = get_artifact(file_id)
                if not item:
                    raise ValueError("文件已不可用")
                refs.append({
                    "file_id": file_id, "name": item.get("name"),
                    "mime_type": item.get("mime_type"), "size": item.get("size"),
                    "storage_key": item.get("storage_key"), "url": f"/files/{file_id}",
                })
            with SessionLocal() as db:
                persist_artifacts(db, scope.owner_user_id, scope.chat_id, refs, commit=False)
                for ref in refs:
                    row = db.get(Artifact, ref["file_id"])
                    if not row or row.user_id != scope.owner_user_id or row.chat_id != scope.chat_id:
                        raise ValueError("文件归属不匹配")
                    row.extra_data = {
                        **(row.extra_data or {}), "agent_api_key_id": scope.api_key_id,
                        "agent_id": scope.agent_id,
                    }
                db.commit()
            return refs

        try:
            refs = await asyncio.to_thread(persist)
        except (ValueError, OSError) as exc:
            return _resp_json({"error": str(exc)})
        workspace.mark_active()
        for ref in refs:
            workspace.pin(
                file_id=ref["file_id"], name=ref["name"], mime_type=ref["mime_type"],
                size=ref["size"], url=ref["url"],
            )
        return _resp_json({"ok": True, "pinned": refs, "pinned_count": len(workspace.get_pinned_file_ids())})

    async def sandbox_put_artifact(artifact_id: str, dest_path: str):
        """将当前 API 会话的文件放入其独立沙箱。"""
        try:
            provider = _provider()
            path = _workspace_path(dest_path)
            if not await asyncio.to_thread(artifact_in_scope, artifact_id, scope):
                raise ValueError("文件不属于当前 API 会话或已删除")
            from core.llm.tools._tool_helpers import _resolve_artifact_files
            files, error = await asyncio.to_thread(
                _resolve_artifact_files, {path: artifact_id}, scope.owner_user_id,
            )
            if error or not files:
                return _resp_json({"error": error or "文件不可用"})
            content = base64.b64decode(files[path])
            await provider.put_file(
                scope.sandbox_session_id, path, content, user_id=scope.sandbox_user_id,
            )
            return _resp_json({"ok": True, "artifact_id": artifact_id, "dest_path": path})
        except (ValueError, OSError, SandboxError) as exc:
            return _resp_json({"error": str(exc)})

    async def sandbox_get_artifact(src_path: str, name: str = ""):
        """保存当前 API 沙箱中的产物并返回文件 ID；不读取个人空间。"""
        from core.config.settings import settings
        from core.llm.tools._tool_helpers import _store_generated_file_path
        target = None
        try:
            provider = _provider()
            path = _workspace_path(src_path)
            with tempfile.NamedTemporaryFile(prefix="agent-api-", delete=False) as temp:
                target = Path(temp.name)
            size = await provider.get_file_to_path(
                scope.sandbox_session_id, path, target,
                max_bytes=settings.sandbox.artifact_max_bytes,
                user_id=scope.sandbox_user_id,
            )
            if size <= 0:
                raise ValueError("文件为空")
            filename = Path(name or path).name or "output"
            ref = await asyncio.to_thread(
                _store_generated_file_path, target, name=filename,
                mime_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                user_id=scope.owner_user_id, source="sandbox_get_artifact",
                extra_metadata={
                    "chat_id": scope.chat_id, "agent_api_key_id": scope.api_key_id,
                },
            )
            if not ref:
                raise ValueError("产物保存失败")
            return _resp_json({"ok": True, **ref, "artifacts": [ref]})
        except (ValueError, OSError, SandboxError) as exc:
            return _resp_json({"error": str(exc)})
        finally:
            if target is not None:
                target.unlink(missing_ok=True)

    for tool in (read_artifact, sandbox_get_artifact, pin_to_workspace):
        toolkit.register_tool_function(tool, namesake_strategy="override")
    if not read_only:
        for tool in (bash, write_stdin, sandbox_put_artifact):
            toolkit.register_tool_function(tool, namesake_strategy="override")

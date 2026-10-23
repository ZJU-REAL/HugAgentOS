"""Shared helpers for the new file-operation tools."""

from __future__ import annotations


import asyncio
import base64
import json
import logging
import mimetypes
import shlex
from typing import Any, Optional

from agentscope.message import Base64Source, DataBlock, TextBlock

# AgentScope 2.0: tool functions must return ToolChunk; aliased (its fields are a superset of ToolChunk).
from agentscope.tool._response import ToolChunk
from core.projects.edition import ProjectScope

logger = logging.getLogger(__name__)


async def resp_image(
    image_bytes: bytes, mime_type: str, *, name: str, meta: Optional[dict[str, Any]] = None
) -> ToolChunk:
    """图片原样交给多模态主模型：一段 JSON 元信息 + 一个图片数据块。

    AgentScope 会把工具结果里的 DataBlock 提升成下一轮的 image 消息；端点不收图时
    ``chat_models`` 有转写/剥离兜底。base64 编码在线程里做，和上传图片那条路一样
    不占事件循环。
    """
    payload = {"type": "image", "mime_type": mime_type, "size": len(image_bytes), **(meta or {})}
    data = await asyncio.to_thread(lambda: base64.b64encode(image_bytes).decode("ascii"))
    return ToolChunk(
        content=[
            *resp_json(payload).content,
            DataBlock(
                type="data",
                source=Base64Source(type="base64", media_type=mime_type, data=data),
                name=name,
            ),
        ]
    )


def resp_json(payload: dict[str, Any]) -> ToolChunk:
    """Wrap a JSON dict as a single-text-block ToolChunk.

    Mirrors ``core.llm.tool._resp_json``. Re-implemented here so this package
    has zero cross-imports back to the large monolithic ``tool.py``.
    """
    return ToolChunk(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    )


def response_metadata(response: ToolChunk, metadata: dict[str, Any]) -> ToolChunk:
    """Merge file metadata into the existing JSON block, preserving image layout."""
    block = response.content[0]
    block.text = json.dumps({**json.loads(block.text), **metadata}, ensure_ascii=False)
    return response


def resolve_sandbox_session(
    sandbox_session_id: Optional[str],
    chat_id: Optional[str],
) -> Optional[str]:
    """Resolve the one sandbox identity for the current conversation.

    A non-empty explicit override wins. Missing or empty overrides fall back to
    ``chat_id`` so callers cannot accidentally create a second sandbox inside
    the same conversation.
    """
    return sandbox_session_id or chat_id




async def sandbox_exec_bash(
    script: str,
    *,
    chat_id: Optional[str],
    user_id: Optional[str] = None,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a bash script in the sandbox and return ``(exit_code, stdout, stderr)``.

    Wraps ``provider.run_to_completion`` for the Glob/Grep tools. Errors are
    surfaced as ``(exit_code=-1, stdout="", stderr=str(exc))``.

    NOTE: ``chat_id`` here is the *sandbox session id* — callers pass the
    resolved ``_sess`` (``sandbox_session_id`` or chat_id fallback), never a
    DB-scoping chat id. Kept named ``chat_id`` to avoid churning call sites.

    ``user_id`` 必须传：沙箱首次执行时靠它建 ``/myspace`` 软链，漏传会让这个沙箱里
    所有 ``/myspace/...`` 路径都不存在。
    """
    from core.sandbox import ProcessRequest, SandboxConnectError, SandboxError, get_sandbox_provider

    try:
        provider = get_sandbox_provider()
        result = await provider.run_to_completion(
            ProcessRequest(
                script_content=script,
                script_name="_tool_helper.sh",
                language="bash",
                timeout=max(1, min(int(timeout or 30), 60)),
                session_id=chat_id,
                user_id=user_id,
            )
        )
        return result.exit_code, result.stdout, result.stderr
    except (SandboxError, SandboxConnectError) as exc:
        return -1, "", str(exc)


def shell_quote(value: str) -> str:
    """Shell-quote a value for safe interpolation into a bash command."""
    return shlex.quote(value)


def register_file_artifact(
    *, user_id: str, chat_id: Optional[str], filename: str,
    content: bytes, scope: Optional[ProjectScope] = None,
) -> Optional[dict[str, Any]]:
    """Create an immutable downloadable snapshot of a sandbox file."""
    from core.files.artifacts.generated import store_generated_files as _store_generated_files

    if not user_id or not filename:
        return None
    name = filename.rsplit("/", 1)[-1]
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    refs = _store_generated_files(
        [{"name": name, "size": len(content),
          "content_b64": base64.b64encode(content).decode("ascii"), "mime_type": mime}],
        user_id=user_id, source="sandbox",
        extra_metadata={"chat_id": chat_id} if chat_id else None,
    )
    return dict(refs[0]) if refs else None


def pin_artifact_to_workspace(ref: dict[str, Any]) -> bool:
    """Pin an artifact ref into the per-run workspace state.

    The frontend's "attachment card" rendering is gated by ``workspace_files``
    (see MessageBubble.renderArtifactCards). Without a pin, an in-place Edit or
    Write produces no visible card in the current turn — the file_id is the same
    as before, ``workspaceFiles`` is empty for this turn, and the user has no
    visual confirmation of the change. Pinning right after the write closes that
    gap without waiting for the model to call ``pin_to_workspace`` itself.

    **只在写文件的那一轮的异步上下文里挂得上**：ContextVar 那份状态就是"当前这一轮"，
    卡片因此落在真正写入文件的会话里。没有当前轮次上下文时，不自动挂载卡片。
    """
    file_id = ref.get("file_id") if isinstance(ref, dict) else None
    if not file_id:
        return False
    try:
        from core.llm.runtime import workspace as _workspace

        pinned = _workspace.pin(
            file_id=str(file_id),
            name=ref.get("name"),
            mime_type=ref.get("mime_type"),
            size=ref.get("size"),
            url=ref.get("url"),
        )
        if pinned:
            _workspace.mark_active()
        return pinned
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artifact-sync] pin_to_workspace failed for %s: %s", file_id, exc)
        return False

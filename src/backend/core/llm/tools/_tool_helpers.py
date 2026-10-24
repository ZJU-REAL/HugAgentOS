"""Shared low-level helpers for the LLM tool registrations.

Extracted from core/llm/tool.py so per-tool modules and the remaining register_*
functions share them. ``core.llm.tool`` re-exports these for compatibility.
"""

import json
import logging
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool._response import ToolChunk
from core.config.settings import settings

logger = logging.getLogger(__name__)













def resp_json(payload: dict[str, Any]) -> ToolChunk:
    """Helper: wrap a JSON-serializable dict as a single-text-block ToolChunk."""
    return ToolChunk(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    )


def validate_workspace_path(
    path: str,
    *,
    root: str | None = None,
    additional_roots: tuple[str, ...] = (),
) -> str | None:
    """路径必须落在工作目录内（或调用方显式给出的其它目录内）。

    ``root`` 是这次调用的工作目录——本机形态下就是这个会话自己的目录。不传时退回
    部署级的工作区根（容器里会话隔离由执行服务自己做）。``additional_roots`` 留给
    "用户显式指定"的位置：绑定的本地项目文件夹、已授权的目录。

    返回错误字符串表示拒绝，``None`` 表示通过。
    """
    from core.sandbox.workspace.paths import WORKSPACE

    base = root or WORKSPACE
    if not path or not isinstance(path, str):
        return "path 必须为非空字符串"
    import ntpath
    import posixpath

    windows = bool(ntpath.splitdrive(base)[0])
    pathmod = ntpath if windows else posixpath
    parts = path.replace(chr(92), "/").split("/")
    if ".." in parts:
        return f"path 不允许包含 ..: {path}"
    target = pathmod.normcase(pathmod.normpath(path))
    for allowed in (base, *additional_roots):
        allowed_root = pathmod.normcase(pathmod.normpath(allowed))
        try:
            if pathmod.commonpath([allowed_root, target]) == allowed_root:
                return None
        except ValueError:
            continue
    return f"path 必须在 {base}/ 下: {path}"

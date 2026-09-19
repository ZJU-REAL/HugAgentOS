"""Skill-loading agent tools.

Tools that let the model discover and load Agent Skills from the sandboxed
skill directories: a path-guarded ``view_text_file`` (which also injects the
runtime hint when a ``SKILL.md`` is read) and a deprecated ``use_skill`` stub
that redirects the model back to ``view_text_file``.

Relocated from the former ``core.llm.tool`` module so the singular ``tool.py``
no longer coexists with this ``tools/`` package.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import Toolkit

# AgentScope 2.0: tool functions must return ToolChunk (call_tool rejects ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse

from core.agent_skills.loader import get_skill_loader

logger = logging.getLogger(__name__)


def _resolve_skill_path(file_path: str, loader: Any = None) -> str | None:
    from ._paths import path_rules
    return path_rules().resolve_skill_file(file_path, loader)


def _extract_skill_id_from_skill_file(file_path: str) -> str | None:
    """Best-effort skill id extraction from a resolved SKILL.md path."""
    path = Path(file_path)
    if path.name != "SKILL.md":
        return None
    skill_id = path.parent.name.strip()
    return skill_id or None


def _build_skill_bash_hint(
    loader: Any,
    skill_id: str,
    skill_dir: str,
) -> str | None:
    """Tell the model the skill is ready and can be invoked via the bash tool.

    Difference from the old ``_build_skill_script_runtime_hint``:
    - No longer enumerates ``executable_scripts`` —— script invocation all goes through bash + relative paths
    - No longer requires the two parameters ``skill_id``/``script_name`` —— bash assembles the command directly
    - Adds a getting-started hint for ``sandbox_put_artifact`` / ``sandbox_get_artifact``
    """
    spec = loader.load_skill_full(skill_id)
    if not spec:
        return None

    name = getattr(spec, "name", skill_id) or skill_id
    from core.agent_skills.config import model_facing_skill_dir

    sandbox_dir = model_facing_skill_dir(skill_id, skill_dir)

    from ._paths import path_rules

    quote = path_rules().quote_shell_path
    scripts = list(getattr(spec, "executable_scripts", None) or [])
    first = (scripts[0].get("name") or "").strip() if scripts else ""
    if first:
        interpreter = "bash" if first.endswith(".sh") else "python"
        example_cmd = f"{interpreter} {quote(sandbox_dir + '/' + first)}"
    else:
        example_cmd = f"ls -- {quote(sandbox_dir)}"

    lines = [
        "",
        "----- Runtime Hint -----",
        f"当前已加载技能：{name}",
        f"技能目录：{sandbox_dir}/。脚本使用绝对路径启动，输入输出相对当前工作目录。",
        "调用方式：使用 `bash` 工具，按 SKILL.md 给出的命令拼接，例如：",
        f"  bash(command={json.dumps(example_cmd, ensure_ascii=False)})",
        "若用户上传的文件需要传给脚本：",
        '  sandbox_put_artifact(artifact_id="ua_xxx", dest_path="input.docx")',
        "脚本产出文件后，登记成可下载的 artifact：",
        '  sandbox_get_artifact(src_path="output.docx")',
    ]
    return "\n".join(lines)


def register_sandboxed_view_text_file(
    toolkit: Toolkit,
    allowed_dirs: list[str],
    loader: Any,
    loaded_skill_ids: set[str] | None = None,
) -> None:
    """Register a sandboxed view_text_file tool."""
    import os as _os

    resolved_dirs = [_os.path.realpath(d) for d in allowed_dirs]

    async def view_text_file(
        file_path: str,
        ranges: list[int] | None = None,
    ) -> ToolResponse:
        """View file content within allowed skill directories."""
        prepared = getattr(loader, "capability_run", None)
        if prepared is not None:
            from core.capabilities.runtime import validate
            import asyncio
            from core.capabilities.errors import CapabilityError

            try:
                # Check only the file's skill. An unrelated skill cannot block this read.
                requested_name = _extract_skill_id_from_skill_file(file_path)
                if requested_name not in prepared.bindings:
                    requested_name = next(
                        (
                            name
                            for name in prepared.bindings
                            if _os.path.realpath(file_path).startswith(
                                _os.path.realpath(loader.get_skill_dir(name) or "") + _os.sep
                            )
                        ),
                        None,
                    )
                # 路径落不到任何一个已授权技能上：交给下面的目录白名单判定，
                # 由它给出明确的拒绝，而不是含糊地说「技能暂不可用」。
                if requested_name is not None:
                    await asyncio.to_thread(validate, prepared, only_skill=requested_name)
            except (CapabilityError, OSError):
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text="该技能当前不可读取或已停用；未执行该能力。请向用户说明并继续处理可完成的部分。",
                        )
                    ]
                )
            mapped = _resolve_skill_path(file_path, loader)
            if mapped:
                file_path = mapped
        real = _os.path.realpath(_os.path.expanduser(file_path))

        if not _os.path.exists(real):
            resolved = _resolve_skill_path(file_path, loader)
            if resolved:
                file_path = resolved
                real = _os.path.realpath(resolved)

        if not any(real.startswith(d + _os.sep) or real == d for d in resolved_dirs):
            return ToolResponse(content=[TextBlock(
                type="text",
                text=f"Error: Access denied. Only files inside skill directories can be read.\nRequested: {file_path}",
            )])

        # AgentScope 2.0: the private agentscope.tool._text_file subpackage has been removed. Inline an
        # equivalent text-file read (optional [start, end] 1-indexed line range).
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as _f:
                _lines = _f.readlines()
            if ranges and isinstance(ranges, list) and len(ranges) >= 2:
                _start = max(1, int(ranges[0]))
                _end = min(len(_lines), int(ranges[1]))
                _selected = _lines[_start - 1:_end]
            else:
                _selected = _lines
            _text = "".join(_selected)
        except Exception as _e:  # noqa: BLE001
            return ToolResponse(content=[TextBlock(
                type="text", text=f"Error reading file: {_e}",
            )])
        resp = ToolResponse(content=[TextBlock(type="text", text=_text)])

        if _os.path.basename(real) == "SKILL.md":
            skill_dir = _os.path.dirname(real)
            skill_id = _extract_skill_id_from_skill_file(file_path)
            if prepared is not None:
                skill_id = next((name for name in prepared.bindings
                                 if _os.path.realpath(loader.get_skill_dir(name)) == skill_dir), None)
            if loaded_skill_ids is not None and skill_id:
                loaded_skill_ids.add(skill_id)
            for i, block in enumerate(resp.content):
                text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                if "{baseDir}" in text:
                    from core.agent_skills.config import model_facing_skill_dir
                    directory = model_facing_skill_dir(skill_id or "", skill_dir).replace(chr(92), "/")
                    resp.content[i] = TextBlock(type="text", text=text.replace("{baseDir}", directory))
            if skill_id:
                runtime_hint = _build_skill_bash_hint(loader, skill_id, skill_dir)
                if runtime_hint:
                    resp.content.append(TextBlock(type="text", text=runtime_hint))

                # ── Observability: record skill auto-load via view_text_file ──
                try:
                    from core.services import log_service as _lw
                    spec = loader.load_skill_full(skill_id)
                    source = loader.get_skill_source(skill_id)
                    if prepared is not None and skill_id in prepared.bindings:
                        from core.capabilities.ref import LOCAL_PROFILE, BUILTIN_PROFILE
                        source = "local" if prepared.bindings[skill_id]["profile"] in (LOCAL_PROFILE, BUILTIN_PROFILE) else "cloud"
                    _lw.schedule_skill_call_write({
                        "skill_id": skill_id,
                        "skill_name": getattr(spec, "name", skill_id) if spec else skill_id,
                        "skill_version": getattr(spec, "version", None) if spec else None,
                        "skill_source": source,
                        "invocation_type": "view",
                        "script_name": None,
                        "status": "success",
                    })
                except Exception:
                    logger.debug("skill view log failed", exc_info=True)

        return resp

    toolkit.register_tool_function(view_text_file, namesake_strategy="override")

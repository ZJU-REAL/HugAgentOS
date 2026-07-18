"""Project-mode system-prompt section rendering.

Extracted from prompts/prompt_runtime.py. Imports the DB-parts loader from
prompt_runtime at module top (the cycle resolves: prompt_runtime defines
_load_db_prompt_parts before importing this module). prompt_runtime re-exports
these names for backward compatibility.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from prompts.prompt_runtime import _load_db_prompt_parts  # noqa: E402 (cycle-safe)


def _format_size(n: int) -> str:
    """Human-readable byte size (1.2 MB / 480 KB / 12 B)."""
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f} MB"
    return f"{n/1024/1024/1024:.1f} GB"


# Hard cap on the file listing in the project-mode prompt: beyond it only the first
# N are listed, with a hint to query the rest via tools
_PROJECT_FILE_LIST_CAP = 50

# part_id of the project-mode section in AdminPromptPart. Editable/versionable in
# the Config console; falls back to _PROJECT_MODE_DEFAULT_TEMPLATE below when
# missing from the DB.
PROJECT_MODE_PART_ID = "project_mode"
PROJECT_MODE_DISPLAY_NAME = "项目模式段（动态附加）"

# Default template. {var} placeholders go through prompts.provider.render_template
# (str.format_map).
# Available variables:
#   {project_name}        project name (already replaced with "(未命名项目)" when empty)
#   {folder_name}         bound folder name (may be empty)
#   {folder_scope_text}   "我的空间" (My Space) / "团队空间" (team space)
#   {folder_scope_block}  pre-rendered "bound to folder xx..." section (empty without a folder)
#   {file_count}          total file count (int)
#   {file_list_block}     pre-rendered "### project sandbox file list..." section (empty without files)
#   {instructions_block}  pre-rendered "### project instructions..." section (empty without instructions)
_PROJECT_MODE_DEFAULT_TEMPLATE = """## 项目模式
本次对话挂载在项目「{project_name}」下。
{folder_scope_block}
{file_list_block}
{instructions_block}"""


def _render_file_list_block(files: list, total: int) -> str:
    """Markdown section for the file listing (heading + entries + truncation notice + hint). Returns '' for an empty list."""
    if total <= 0:
        return ""
    shown = min(total, _PROJECT_FILE_LIST_CAP)
    lines: list[str] = [f"### 项目沙盒文件清单（共 {total} 个，列出前 {shown}）"]
    for item in files[:_PROJECT_FILE_LIST_CAP]:
        rel = (item.get("name") or "").strip()  # the name returned by the service already includes the subpath
        if not rel:
            continue
        mime = (item.get("mime_type") or "").strip()
        size = _format_size(item.get("size_bytes") or 0)
        meta = f"{mime}, {size}" if mime else size
        lines.append(f"- {rel} ({meta})")
    if total > _PROJECT_FILE_LIST_CAP:
        lines.append(
            f"...还有 {total - _PROJECT_FILE_LIST_CAP} 个未列出，"
            "用 list_myspace_files 工具查看完整列表。"
        )
    lines.append(
        "用户提到的文件名默认指上面这些文件——直接用 Read/Edit/Glob/Grep 等工具操作即可。"
    )
    return "\n".join(lines)


def _render_folder_scope_block(folder_name: str, folder_scope_text: str, file_count: int) -> str:
    """Bound-folder description section. Returns '' without a folder_name."""
    f = (folder_name or "").strip()
    if not f:
        return ""
    base = (
        f"该项目挂钩到{folder_scope_text}的「{f}」文件夹。"
        f"项目相关的文件读写应当严格限定在 `/myspace/{f}/`（及其子文件夹）下，"
        f"不要把项目无关的文件写到此处，也不要假设其它路径下的文件属于本项目。"
    )
    if file_count <= 0:
        base += "\n该项目沙盒目前还没有任何文件；用户可能在对话中补充上传。"
    return base


def _render_instructions_block(instructions: str) -> str:
    """Project-instructions section. Returns '' without instructions."""
    s = (instructions or "").strip()
    if not s:
        return ""
    return "### 项目指令（用户提供，优先级最高）\n" + s


def _collapse_blanks(text: str) -> str:
    """Collapse 3+ consecutive blank lines → 2; common after empty block substitution in the template."""
    import re
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _get_project_mode_template() -> str:
    """Load the project_mode template.

    Lookup order:
      1) an override with the same part_id in the AdminPromptPart table (legacy path, backward compat)
      2) project_mode among the parts of the active 'system' version (Config console edit entry)
      3) _PROJECT_MODE_DEFAULT_TEMPLATE (hard-coded default)
    """
    # 1) AdminPromptPart override
    parts = _load_db_prompt_parts()
    row = parts.get(PROJECT_MODE_PART_ID)
    if row and row.get("is_enabled", True):
        content = (row.get("content") or "").strip()
        if content:
            return content
    # 2) parts of the active system version (edited in the admin UI)
    try:
        from core.services import prompt_version_service as pvs
        active = pvs.get_active_version("system")
        if active:
            for p in active.get("parts") or []:
                if (p.get("part_id") or "").strip() != PROJECT_MODE_PART_ID:
                    continue
                if not p.get("is_enabled", True):
                    continue
                content = (p.get("content") or "").strip()
                if content:
                    return content
    except Exception:
        pass
    # 3) default
    return _PROJECT_MODE_DEFAULT_TEMPLATE


def _build_project_section(
    *,
    project_name: str,
    project_instructions: str,
    folder_name: str,
    folder_kind: str,
    project_files: list | None = None,
) -> str:
    """Build the "project mode" system-prompt section.

    A project is essentially a view over a MySpace folder. A chat mounted on a
    project = the agent's file read/write scope is confined to that folder subtree.

    The template has two layers:
    - the outer frame (heading, assembly order of the sections) goes through
      ``AdminPromptPart(part_id='project_mode')``, editable/versionable in the
      Config console;
    - the file listing / folder-binding section / project-instructions section are
      pre-rendered into strings in Python and injected via the
      ``{file_list_block}`` / ``{folder_scope_block}`` / ``{instructions_block}``
      variables, working around str.format_map's lack of loops/conditionals.
    """
    from prompts.provider import render_template

    name = (project_name or "").strip() or "(未命名项目)"
    files = list(project_files or [])
    total = len(files)
    scope_text = "我的空间" if folder_kind == "personal" else "团队空间"

    vars_ = {
        "project_name": name,
        "folder_name": (folder_name or "").strip(),
        "folder_scope_text": scope_text,
        "folder_scope_block": _render_folder_scope_block(folder_name, scope_text, total),
        "file_count": total,
        "file_list_block": _render_file_list_block(files, total),
        "instructions_block": _render_instructions_block(project_instructions),
    }
    template = _get_project_mode_template()
    rendered = render_template(template, vars=vars_, strict=False)
    return _collapse_blanks(rendered)

"""Project-mode system-prompt section rendering.

Extracted from prompts/prompt_runtime.py. Imports the DB-parts loader from
prompt_runtime at module top (the cycle resolves: prompt_runtime defines
_load_db_prompt_parts before importing this module). prompt_runtime re-exports
these names for backward compatibility.
"""

from __future__ import annotations

import os
from core.llm import prompt_versions as prompt_versions_module

from prompts.prompt_runtime import _load_db_prompt_parts  # noqa: E402 (cycle-safe)


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
#   {folder_scope_text}   edition-specific folder label
#   {folder_scope_block}  pre-rendered "bound to folder xx..." section (empty without a folder)
#   {instructions_block}  pre-rendered "### project instructions..." section (empty without instructions)
_PROJECT_MODE_DEFAULT_TEMPLATE = """## 项目模式
本次对话挂载在项目「{project_name}」下。
{folder_scope_block}
{instructions_block}"""


def _render_folder_scope_block(folder_name: str, folder_scope_text: str, workspace_path: str = "") -> str:
    """Bound-folder description section. Returns '' without a folder_name."""
    f = (folder_name or "").strip()
    if not f:
        return ""
    path = workspace_path or f"/myspace/{f}/"
    base = (
        f"该项目挂钩到{folder_scope_text}的「{f}」文件夹。"
        f"项目相关的文件读写应当严格限定在 `{path}`（及其子文件夹）下，"
        f"不要把项目无关的文件写到此处，也不要假设其它路径下的文件属于本项目。"
    )
    base += "\n需要了解项目文件时，使用目录、搜索和读取工具按需查看。"
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
        from core.llm import prompt_versions as pvs
        active = prompt_versions_module.get_active_version("system")
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
    workspace_path: str = "",
) -> str:
    """Build the "project mode" system-prompt section.

    A project is essentially a view over a MySpace folder. A chat mounted on a
    project = the agent's file read/write scope is confined to that folder subtree.

    The template has two layers:
    - the outer frame (heading, assembly order of the sections) goes through
      ``AdminPromptPart(part_id='project_mode')``, editable/versionable in the
      Config console;
    - the folder-binding section and project-instructions section are
      pre-rendered into strings in Python and injected via the
      ``{folder_scope_block}`` / ``{instructions_block}``
      variables, working around str.format_map's lack of loops/conditionals.
    """
    from prompts.provider import render_template

    name = (project_name or "").strip() or "(未命名项目)"
    from core.files.edition import folder_scope_label
    scope_text = folder_scope_label(folder_kind)


    vars_ = {
        "project_name": name,
        "folder_name": (folder_name or "").strip(),
        "folder_scope_text": scope_text,
        "folder_scope_block": _render_folder_scope_block(folder_name, scope_text, workspace_path),
        "instructions_block": _render_instructions_block(project_instructions),
    }
    template = _get_project_mode_template()
    rendered = render_template(template, vars=vars_, strict=False)
    if workspace_path:
        rendered += f"\n当前项目工作目录：`{workspace_path}`（已挂载目录的子目录，无需暂存）。"
    return _collapse_blanks(rendered)


def _build_local_project_section(
    *,
    project_name: str,
    project_instructions: str,
    local_path: str,
    local_slug: str,
    inherited_instructions=(),
) -> str:
    """Build the desktop **local-mode** project section (ticket #05).

    Unlike the cloud project section (a view over a MySpace folder), a local
    project is the user's real folder on their own computer. The agent works in
    it directly — no upload — but must respect the authorization + confirmation
    boundary enforced by the execution policy gate (ticket #07). Only injected
    when ``project_is_local`` is set, so the cloud/web deployment never sees it.

    ``inherited_instructions`` are outer ``(path, text)`` AGENTS.md files, outermost
    first; when present every file, the project root last, is labelled with its path.
    """
    from prompts.desktop_templates import render_desktop_part
    variables = dict(project_name=(project_name or "").strip(), local_path=(local_path or "").strip())
    section = render_desktop_part("project" if variables["local_path"] else "project_missing", **variables)
    instructions = project_instructions.strip()
    blocks = [f"#### `{path}`\n{text.strip()}" for path, text in inherited_instructions or () if text.strip()]
    if blocks and instructions:
        root = os.path.join(variables["local_path"], "AGENTS.md")
        blocks.append(f"#### `{root}`\n{instructions}")
    if blocks:
        instructions = "\n\n".join(blocks)
    if instructions:
        section += "\n\n" + render_desktop_part("project_instructions", instructions=instructions)
    return _collapse_blanks(section)

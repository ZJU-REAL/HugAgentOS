"""Agent skills registry (skill-creator aligned).

Each skill lives in:
  skill_bundles/<skill-id>/SKILL.md

The loader reads SKILL.md frontmatter + body instructions so models can reliably
load and apply enabled skills at runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skill_bundles"
_ID_RE = re.compile(r"^[a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class AgentSkillMetadata:
    """Lightweight skill metadata (no instructions loaded)."""
    id: str
    name: str
    description: str
    version: str
    tags: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)  # Added for tool filtering
    mcp_server_ids: List[str] = field(default_factory=list)
    skill_path: str = ""


@dataclass(frozen=True)
class AgentSkillSpec:
    """Full skill spec with instructions."""
    id: str
    name: str
    description: str
    version: str
    instructions: List[str] = field(default_factory=list)
    inputs: str = ""
    outputs: str = ""
    tags: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)  # Tool filtering support
    mcp_server_ids: List[str] = field(default_factory=list)
    extra_files: List[str] = field(default_factory=list)    # Available resource file names
    base_dir: str = ""                                      # Materialized directory path (for {baseDir} substitution)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    executable_scripts: List[Dict[str, Any]] = field(default_factory=list)  # Scripts declared in _scripts.json
    skill_path: str = ""


class SkillSpecError(ValueError):
    pass


def _require_id(value: str) -> str:
    v = (value or "").strip()
    if not _ID_RE.match(v):
        raise SkillSpecError(f"invalid skill id: {v!r} (expect lowercase/digits/hyphen)")
    return v


def _split_frontmatter(raw: str) -> Tuple[Dict[str, str], str]:
    text = raw or ""
    if not text.startswith("---\n"):
        raise SkillSpecError("SKILL.md missing YAML frontmatter")

    end_idx = text.find("\n---\n", 4)
    if end_idx < 0:
        raise SkillSpecError("SKILL.md frontmatter not closed")

    fm_raw = text[4:end_idx]
    body = text[end_idx + len("\n---\n") :]
    frontmatter: Dict[str, str] = {}

    lines = fm_raw.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        k, v = stripped.split(":", 1)
        key = k.strip()
        val = v.strip()
        # YAML block scalar: ``key: >`` (folded) or ``key: |`` (literal), with
        # optional chomping/indent indicator (>- |+ …). The value is the set of
        # following lines indented deeper than the key. The old parser stored just
        # ">" / "|" here, which is how ppt-master's multi-line ``description: >``
        # showed up as a bare ">" — losing the skill's whole selection signal.
        if val and val[0] in "|>":
            folded = val[0] == ">"
            block: List[str] = []
            i += 1
            while i < n:
                bl = lines[i]
                if not bl.strip():
                    block.append("")
                    i += 1
                    continue
                if (len(bl) - len(bl.lstrip())) <= indent:
                    break
                block.append(bl.strip())
                i += 1
            if folded:
                value = " ".join(x for x in block if x).strip()
            else:
                value = "\n".join(block).strip()
            frontmatter[key] = value
            continue
        frontmatter[key] = val.strip("'").strip('"')
        i += 1
    return frontmatter, body


def _extract_section_text(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?im)^##\s+{re.escape(heading)}\s*$\n(?P<content>.*?)(?=\n##\s+|\Z)",
        re.S,
    )
    m = pattern.search(body)
    if not m:
        return ""
    return m.group("content").strip()


def _extract_instruction_lines(body: str) -> List[str]:
    # Preferred: read from "## Instructions" section.
    section = _extract_section_text(body, "Instructions")
    source = section if section else body
    out: List[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # markdown list item: "- xxx" / "1. xxx"
        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            out.append(m.group(1).strip())
            continue
        m = re.match(r"^\d+\.\s+(.+)$", stripped)
        if m:
            out.append(m.group(1).strip())
    if not out:
        # fallback: first short paragraph line
        for line in source.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                out.append(stripped)
                break
    return out


def _parse_tags(frontmatter: Dict[str, str], body: str) -> List[str]:
    raw = frontmatter.get("tags", "")
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    # Optional body section.
    sec = _extract_section_text(body, "Tags")
    if not sec:
        return []
    tags: List[str] = []
    for line in sec.splitlines():
        stripped = line.strip()
        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            tags.append(m.group(1).strip())
    return tags


def _parse_allowed_tools(fm: Dict[str, str]) -> List[str]:
    """Parse allowed_tools from frontmatter (space-separated string).

    Examples:
        allowed_tools: search database chart
        allowed_tools: web_search, database_query

    Args:
        fm: Frontmatter dictionary.

    Returns:
        List of tool names.
    """
    raw = fm.get("allowed_tools", "") or fm.get("allowed-tools", "")
    if not raw:
        return []

    # Split by space or comma, strip whitespace
    tools = []
    for tool in raw.replace(",", " ").split():
        tool = tool.strip()
        if tool:
            tools.append(tool)
    return tools


def _parse_mcp_server_ids(fm: Dict[str, str]) -> List[str]:
    """Parse MCP server bindings persisted by the skill creation form."""
    raw = fm.get("mcp_servers", "") or fm.get("mcp-server-ids", "")
    if not raw:
        return []
    return list(
        dict.fromkeys(item.strip() for item in raw.replace(",", " ").split() if item.strip())
    )


def _load_skill_metadata_from_str(content: str, skill_id: str) -> AgentSkillMetadata:
    """Load only metadata from SKILL.md content string (for DB-backed skills)."""
    fm, body = _split_frontmatter(content)

    skill_id = _require_id(fm.get("name", skill_id))
    name = fm.get("display_name", "") or skill_id
    description = (fm.get("description", "") or "").strip()
    if not description:
        raise SkillSpecError(f"skill {skill_id!r}: missing frontmatter `description`")
    version = (fm.get("version", "") or "1.0.0").strip()
    tags = _parse_tags(fm, body)
    allowed_tools = _parse_allowed_tools(fm)
    mcp_server_ids = _parse_mcp_server_ids(fm)

    return AgentSkillMetadata(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        tags=tags,
        allowed_tools=allowed_tools,
        mcp_server_ids=mcp_server_ids,
        skill_path=f"db:admin/{skill_id}",
    )


def _load_skill_from_str(content: str, skill_id: str) -> AgentSkillSpec:
    """Load full skill spec from SKILL.md content string (for DB-backed skills)."""
    fm, body = _split_frontmatter(content)

    skill_id = _require_id(fm.get("name", skill_id))
    name = fm.get("display_name", "") or skill_id
    description = (fm.get("description", "") or "").strip()
    if not description:
        raise SkillSpecError(f"skill {skill_id!r}: missing frontmatter `description`")
    version = (fm.get("version", "") or "1.0.0").strip()
    instructions = _extract_instruction_lines(body)
    if not instructions:
        raise SkillSpecError(f"skill {skill_id!r}: skill instructions cannot be empty")

    inputs = _extract_section_text(body, "Inputs")
    outputs = _extract_section_text(body, "Outputs")
    tags = _parse_tags(fm, body)
    allowed_tools = _parse_allowed_tools(fm)
    mcp_server_ids = _parse_mcp_server_ids(fm)

    return AgentSkillSpec(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        instructions=instructions,
        inputs=inputs,
        outputs=outputs,
        tags=tags,
        allowed_tools=allowed_tools,
        mcp_server_ids=mcp_server_ids,
        examples=[],
        skill_path=f"db:admin/{skill_id}",
    )


def _load_skill_metadata_from_file(path: Path) -> AgentSkillMetadata:
    """Load only metadata from SKILL.md (fast, no body parsing)."""
    raw = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(raw)

    folder_id = _require_id(path.parent.name)
    skill_id = _require_id(fm.get("name", folder_id))
    name = fm.get("display_name", "") or skill_id
    description = (fm.get("description", "") or "").strip()
    if not description:
        raise SkillSpecError(f"{path}: missing frontmatter `description`")
    version = (fm.get("version", "") or "1.0.0").strip()
    tags = _parse_tags(fm, body)
    allowed_tools = _parse_allowed_tools(fm)
    mcp_server_ids = _parse_mcp_server_ids(fm)

    return AgentSkillMetadata(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        tags=tags,
        allowed_tools=allowed_tools,
        mcp_server_ids=mcp_server_ids,
        skill_path=str(path),
    )


def _load_skill_from_file(path: Path) -> AgentSkillSpec:
    """Load full skill spec from SKILL.md (includes instructions)."""
    raw = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(raw)

    folder_id = _require_id(path.parent.name)
    skill_id = _require_id(fm.get("name", folder_id))
    name = fm.get("display_name", "") or skill_id
    description = (fm.get("description", "") or "").strip()
    if not description:
        raise SkillSpecError(f"{path}: missing frontmatter `description`")
    version = (fm.get("version", "") or "1.0.0").strip()
    instructions = _extract_instruction_lines(body)
    if not instructions:
        raise SkillSpecError(f"{path}: skill instructions cannot be empty")

    inputs = _extract_section_text(body, "Inputs")
    outputs = _extract_section_text(body, "Outputs")
    tags = _parse_tags(fm, body)
    allowed_tools = _parse_allowed_tools(fm)
    mcp_server_ids = _parse_mcp_server_ids(fm)

    return AgentSkillSpec(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        instructions=instructions,
        inputs=inputs,
        outputs=outputs,
        tags=tags,
        allowed_tools=allowed_tools,
        mcp_server_ids=mcp_server_ids,
        examples=[],
        skill_path=str(path),
    )


def parse_scripts_json(raw: str) -> List[Dict[str, Any]]:
    """Parse _scripts.json content into a list of script declarations.

    Each entry must have at least ``name`` and ``language``. Unknown keys
    are preserved so callers can access ``params_schema`` etc.

    Args:
        raw: JSON string (should be a JSON array).

    Returns:
        List of validated script declaration dicts.

    Raises:
        SkillSpecError: If *raw* is not a valid JSON array or an entry
            is missing required fields.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillSpecError(f"_scripts.json 解析失败: {exc}") from exc

    if not isinstance(data, list):
        raise SkillSpecError("_scripts.json 必须是 JSON 数组")

    scripts: List[Dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise SkillSpecError(f"_scripts.json[{idx}] 必须是对象")
        if "name" not in item:
            raise SkillSpecError(f"_scripts.json[{idx}] 缺少 name 字段")
        scripts.append({
            "name": item["name"],
            "description": item.get("description", ""),
            "language": item.get("language", "python"),
            "timeout": min(int(item.get("timeout", 30)), 120),
            "params_schema": item.get("params_schema"),
            # "stdin_json" (default): params sent via stdin as JSON
            # "cli_args": params converted to CLI arguments (_args)
            "input_mode": item.get("input_mode", "stdin_json"),
        })
    return scripts


def render_skills_prompt(skills: Sequence[AgentSkillSpec]) -> str:
    """Render skill instructions as prompt appendix."""
    if not skills:
        return ""

    lines: List[str] = []
    for skill in skills:
        lines.append(f"### Skill: {skill.id} (v{skill.version})")
        lines.append(f"- 描述: {skill.description}")
        if skill.inputs:
            lines.append(f"- 输入: {skill.inputs}")
        if skill.outputs:
            lines.append(f"- 输出: {skill.outputs}")
        lines.append("- 执行规范:")
        for idx, item in enumerate(skill.instructions, start=1):
            lines.append(f"  {idx}. {item}")
        if skill.tags:
            lines.append(f"- 标签: {', '.join(skill.tags)}")
        lines.append("")

    return "\n".join(lines).strip()

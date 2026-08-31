"""子智能体 <-> Markdown 互转（YAML frontmatter + 正文即 system prompt）。

对齐 Claude Code / pi agent 一类 harness 的 subagent 文件格式：一个智能体一个
``.md`` 文件，frontmatter 写 name/description/tools/skills 等配置，正文即系统
提示词；批量导出打包为 zip。导入侧同时兼容旧版 JSON 数组导出文件。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from decimal import Decimal
from typing import Any, Dict, List

import yaml

# frontmatter 键 <-> DB 字段（对齐 Claude Code / pi 的 tools / skills / model 惯例）
_ALIAS_TO_FIELD = {
    "tools": "mcp_server_ids",
    "skills": "skill_ids",
    "plugins": "plugin_ids",
    "knowledge_bases": "kb_ids",
    "model": "model_provider_id",
    "enabled": "is_enabled",
}
_FIELD_TO_ALIAS = {v: k for k, v in _ALIAS_TO_FIELD.items()}

_LIST_FIELDS = {"mcp_server_ids", "skill_ids", "plugin_ids", "kb_ids", "suggested_questions"}

# 导出时 frontmatter 的键顺序（DB 字段名，写出时再映射为别名）
_FRONTMATTER_FIELDS = [
    "name",
    "description",
    "avatar",
    "model_provider_id",
    "mcp_server_ids",
    "skill_ids",
    "plugin_ids",
    "kb_ids",
    "temperature",
    "max_tokens",
    "max_iters",
    "timeout",
    "is_enabled",
    "sort_order",
    "welcome_message",
    "suggested_questions",
    "extra_config",
]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_MAX_ZIP_MEMBERS = 500
_MAX_MEMBER_BYTES = 5 * 1024 * 1024


def agent_to_markdown(item: Dict[str, Any]) -> str:
    """把智能体字典序列化为 frontmatter markdown，正文为 system_prompt。"""
    meta: Dict[str, Any] = {}
    for field in _FRONTMATTER_FIELDS:
        val = item.get(field)
        if isinstance(val, Decimal):
            val = float(val)
        if val is None:
            continue
        if not isinstance(val, bool) and val in ("", [], {}):
            continue
        meta[_FIELD_TO_ALIAS.get(field, field)] = val
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    body = str(item.get("system_prompt") or "").strip()
    return f"---\n{front}---\n\n{body}\n"


def parse_agent_markdown(text: str) -> Dict[str, Any]:
    """解析 frontmatter markdown 为导入用的智能体字典。"""
    match = _FRONTMATTER_RE.match(text.strip())
    if not match:
        raise ValueError("Markdown 缺少 YAML frontmatter（--- 分隔块）")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter 不是合法 YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter 必须是键值映射")

    item: Dict[str, Any] = {}
    for key, val in meta.items():
        field = _ALIAS_TO_FIELD.get(key, key)
        if field in _LIST_FIELDS and isinstance(val, str):
            val = [part.strip() for part in val.split(",") if part.strip()]
        item[field] = val
    if not item.get("name"):
        raise ValueError("frontmatter 缺少 name 字段")
    item["system_prompt"] = match.group(2).strip()
    return item


def _safe_filename(name: str, used: set) -> str:
    base = re.sub(r"[^\w一-鿿.-]+", "_", name).strip("_") or "agent"
    candidate = base
    seq = 2
    while candidate in used:
        candidate = f"{base}-{seq}"
        seq += 1
    used.add(candidate)
    return f"{candidate}.md"


def agent_filename(name: str) -> str:
    """单个智能体导出时的 .md 文件名。"""
    return _safe_filename(str(name or ""), set())


def agents_to_zip(items: List[Dict[str, Any]]) -> bytes:
    """把一批智能体打包为 zip（每个智能体一个 .md 文件）。"""
    buf = io.BytesIO()
    used: set = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            filename = _safe_filename(str(item.get("name") or ""), used)
            zf.writestr(filename, agent_to_markdown(item))
    return buf.getvalue()


def parse_agents_upload(filename: str, data: bytes) -> List[Dict[str, Any]]:
    """解析导入文件（.md / .zip / 旧版 .json）为智能体字典列表。"""
    lower = (filename or "").lower()
    if lower.endswith(".md"):
        return [parse_agent_markdown(data.decode("utf-8"))]
    if lower.endswith(".zip"):
        items: List[Dict[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [
                info
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(".md")
                and not info.filename.startswith("__MACOSX")
            ]
            if len(members) > _MAX_ZIP_MEMBERS:
                raise ValueError(f"zip 内 .md 文件超过 {_MAX_ZIP_MEMBERS} 个上限")
            for info in members:
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise ValueError(f"文件过大: {info.filename}")
                try:
                    items.append(parse_agent_markdown(zf.read(info).decode("utf-8")))
                except ValueError as exc:
                    raise ValueError(f"{info.filename}: {exc}") from exc
        if not items:
            raise ValueError("zip 中没有可导入的 .md 智能体文件")
        return items
    if lower.endswith(".json"):
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, list):
            raise ValueError("JSON 格式错误：需要数组")
        return parsed
    raise ValueError("不支持的文件类型，仅支持 .md / .zip / .json")

"""Rewrite stored MCP server bindings: each old id becomes a list of new ids.

Walks the same binding columns as ``retire_chart_mcp`` (agents, agent market
snapshots, chat modes, scheduled tasks, skill allowed_tools and SKILL.md headers).
A replacement list may be empty, which simply removes the old id. Order is kept
and the result is de-duplicated. The caller owns commit/rollback.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa
import yaml

BINDING_COLUMNS = {
    "user_agents": ("mcp_server_ids",),
    "agent_market_submissions": ("bindings_snapshot",),
    "chat_modes": ("mcp_server_ids",),
    "scheduled_tasks": ("enabled_mcp_ids",),
    "admin_skills": ("allowed_tools", "skill_content"),
    "admin_skill_drafts": ("allowed_tools", "skill_content"),
}


def _expand_tokens(tokens, mapping):
    out = []
    for token in tokens:
        for new in mapping.get(token, [token]):
            if new not in out:
                out.append(new)
    return out


def rebind_value(value, mapping):
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item in mapping:
                replacements = list(mapping[item])
            elif isinstance(item, dict) and item.get("id") in mapping:
                replacements = [{**item, "id": new} for new in mapping[item["id"]]]
            else:
                replacements = [rebind_value(item, mapping)]
            for new in replacements:
                if new not in out:
                    out.append(new)
        return out
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in mapping:
                for new in mapping[key]:
                    out.setdefault(new, rebind_value(item, mapping))
            else:
                out[key] = rebind_value(item, mapping)
        return out
    if isinstance(value, str):
        # Older skill metadata stores allowed_tools as whitespace-separated text.
        words = value.split()
        if any(word in mapping for word in words):
            return " ".join(_expand_tokens(words, mapping))
    return value


def _rebind_skill_header(content, mapping):
    if not isinstance(content, str):
        return content
    match = re.match(r"\A---[^\S\n]*\n(.*?)\n---(?=\n|$)", content, re.S)
    if match is None:
        return content

    def rebind_field(field):
        key = field.group(1)
        value = yaml.safe_load(field.group(0))[key]
        raw = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
        tokens = [str(token) for token in raw if token]
        if not any(token in mapping for token in tokens):
            return field.group(0)
        kept = _expand_tokens(tokens, mapping)
        return f"{key}: {' '.join(kept)}" if kept else ""

    header = re.sub(
        r"(?m)^(allowed_tools|allowed-tools|mcp_servers|mcp-server-ids):[^\n]*(?:\n[ \t]+[^\n]*)*",
        rebind_field,
        match.group(1),
    )
    return content[: match.start(1)] + header + content[match.end(1) :]


def rebind_mcp_ids(
    bind: sa.Connection,
    mapping: dict[str, list[str]],
    *,
    tables: set[str] | None = None,
) -> dict[str, int]:
    """Replace every bound server id in ``mapping`` with its new ids; returns rows changed per table.

    ``tables`` limits the rewrite to those binding tables (default: all of them).
    """
    if not mapping:
        return {}
    names = set(sa.inspect(bind).get_table_names())
    metadata = sa.MetaData()
    counts: dict[str, int] = {}
    for name, columns in BINDING_COLUMNS.items():
        if name not in names or (tables is not None and name not in tables):
            continue
        target = sa.Table(name, metadata, autoload_with=bind)
        for column in columns:
            if column not in target.c:
                continue
            conditions = [sa.cast(target.c[column], sa.Text).contains(old) for old in mapping]
            for row in bind.execute(sa.select(target).where(sa.or_(*conditions))).mappings():
                current = row[column]
                rebound = (
                    _rebind_skill_header(current, mapping)
                    if column == "skill_content"
                    else rebind_value(current, mapping)
                )
                if rebound == current:
                    continue
                identity = sa.and_(*(key == row[key.name] for key in target.primary_key))
                values = {column: rebound}
                if "updated_at" in target.c:
                    values["updated_at"] = datetime.now(timezone.utc)
                bind.execute(target.update().where(identity).values(values))
                counts[name] = counts.get(name, 0) + 1
    return counts

"""Remove the retired chart MCP and its live configuration in one transaction.

Used by both Alembic and create_all/local startup. Existing artifacts, execution
history, prompt text and model providers are intentionally outside this operation.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import yaml

CHART_SERVER_ID = "generate_chart_tool"


def _without_servers(value, server_ids):
    if isinstance(value, list):
        return [
            _without_servers(item, server_ids)
            for item in value
            if not (isinstance(item, str) and item in server_ids)
            and not (isinstance(item, dict) and item.get("id") in server_ids)
        ]
    if isinstance(value, dict):
        return {
            key: _without_servers(item, server_ids)
            for key, item in value.items()
            if key not in server_ids
        }
    if isinstance(value, str):
        # Older skill metadata stores allowed_tools as whitespace-separated text.
        words = value.split()
        if any(word in server_ids for word in words):
            return " ".join(word for word in words if word not in server_ids)
    return value


def _clean_skill_header(content, server_ids):
    if not isinstance(content, str):
        return content
    match = re.match(r"\A---[^\S\n]*\n(.*?)\n---(?=\n|$)", content, re.S)
    if match is None:
        return content

    def clean_field(field):
        key = field.group(1)
        value = yaml.safe_load(field.group(0))[key]
        tokens = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
        kept = [token for token in tokens if token and token not in server_ids]
        if kept == [token for token in tokens if token]:
            return field.group(0)
        return f"{key}: {' '.join(kept)}" if kept else ""

    header = re.sub(
        r"(?m)^(allowed_tools|allowed-tools|mcp_servers|mcp-server-ids):[^\n]*(?:\n[ \t]+[^\n]*)*",
        clean_field,
        match.group(1),
    )
    return content[: match.start(1)] + header + content[match.end(1) :]


def retire_chart_mcp(bind: sa.Connection) -> dict[str, int]:
    """Delete only chart configuration; the caller owns commit/rollback."""
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())
    metadata = sa.MetaData()
    tables = {}
    counts = {}

    def table(name):
        if name not in names:
            return None
        if name not in tables:
            tables[name] = sa.Table(name, metadata, autoload_with=bind)
        return tables[name]

    def delete(name, condition):
        target = table(name)
        if target is not None:
            result = bind.execute(target.delete().where(condition(target.c)))
            if result.rowcount:
                counts[name] = result.rowcount

    server_ids = {CHART_SERVER_ID}
    slugs = set()
    versions = table("mcp_market_versions")
    if versions is not None:
        slugs.update(
            bind.execute(
                sa.select(versions.c.slug).where(versions.c.source_server_id == CHART_SERVER_ID)
            ).scalars()
        )
    installs = table("mcp_market_installations")
    if installs is not None:
        slugs.update(
            bind.execute(
                sa.select(installs.c.slug).where(installs.c.server_id == CHART_SERVER_ID)
            ).scalars()
        )
        server_ids.update(
            bind.execute(
                sa.select(installs.c.server_id).where(installs.c.slug.in_(slugs))
            ).scalars()
        )

    delete(
        "mcp_market_installations", lambda c: sa.or_(c.slug.in_(slugs), c.server_id.in_(server_ids))
    )
    delete(
        "marketplace_listing_states",
        lambda c: sa.and_(c.kind == "mcp", c.item_id.in_(slugs | server_ids)),
    )
    delete(
        "mcp_market_submissions",
        lambda c: c.source_server_id.in_(server_ids),
    )
    delete("mcp_market_versions", lambda c: c.slug.in_(slugs))
    delete("mcp_market_items", lambda c: c.slug.in_(slugs))
    delete("admin_mcp_servers", lambda c: c.server_id.in_(server_ids))
    delete("model_role_assignments", lambda c: c.role_key == "chart")
    delete("catalog_overrides", lambda c: sa.and_(c.kind == "mcp", c.item_id.in_(server_ids)))

    bindings = {
        "user_agents": ("mcp_server_ids",),
        "agent_market_submissions": ("bindings_snapshot",),
        "chat_modes": ("mcp_server_ids",),
        "scheduled_tasks": ("enabled_mcp_ids",),
        "admin_skills": ("allowed_tools", "skill_content"),
        "admin_skill_drafts": ("allowed_tools", "skill_content"),
    }
    for name, columns in bindings.items():
        target = table(name)
        if target is None:
            continue
        for column in columns:
            if column not in target.c:
                continue
            conditions = [sa.cast(target.c[column], sa.Text).contains(sid) for sid in server_ids]
            for row in bind.execute(sa.select(target).where(sa.or_(*conditions))).mappings():
                cleaned = (
                    _clean_skill_header(row[column], server_ids)
                    if column == "skill_content"
                    else _without_servers(row[column], server_ids)
                )
                if cleaned != row[column]:
                    identity = sa.and_(*(key == row[key.name] for key in target.primary_key))
                    values = {column: cleaned}
                    if "updated_at" in target.c:
                        values["updated_at"] = datetime.now(timezone.utc)
                    bind.execute(target.update().where(identity).values(values))
                    counts[name] = counts.get(name, 0) + 1

    blocks = table("content_blocks")
    if blocks is not None:
        for row in bind.execute(
            sa.select(blocks.c.id, blocks.c.payload).where(
                blocks.c.id.in_(("catalog", "catalog_overrides", "tool_names", "mcp_tool_names"))
            )
        ).mappings():
            cleaned = _without_servers(row["payload"], server_ids)
            if cleaned != row["payload"]:
                bind.execute(
                    blocks.update().where(blocks.c.id == row["id"]).values(payload=cleaned)
                )
                counts["content_blocks"] = counts.get("content_blocks", 0) + 1
        if counts.get("model_role_assignments"):
            # Existing workers observe committed model changes on their next lookup.
            values = {"payload": {"revision": uuid.uuid4().hex}}
            if "updated_at" in blocks.c:
                values["updated_at"] = datetime.now(timezone.utc)
            if "updated_by" in blocks.c:
                values["updated_by"] = "chart_retirement"
            existing = bind.execute(
                sa.select(blocks.c.id).where(blocks.c.id == "model_config_revision")
            ).first()
            if existing:
                bind.execute(
                    blocks.update().where(blocks.c.id == "model_config_revision").values(**values)
                )
            else:
                bind.execute(blocks.insert().values(id="model_config_revision", **values))
    return counts

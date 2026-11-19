"""Remove the retired chart MCP and its live configuration in one transaction.

Used by both Alembic and create_all/local startup. Existing artifacts, execution
history, prompt text and model providers are intentionally outside this operation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from core.db.rebind_mcp_ids import rebind_value, rebind_mcp_ids

CHART_SERVER_ID = "generate_chart_tool"


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

    # Agents, modes, tasks and skills that named the chart server simply lose it.
    counts.update(rebind_mcp_ids(bind, {server_id: [] for server_id in server_ids}))

    blocks = table("content_blocks")
    if blocks is not None:
        for row in bind.execute(
            sa.select(blocks.c.id, blocks.c.payload).where(
                blocks.c.id.in_(("catalog", "catalog_overrides", "tool_names", "mcp_tool_names"))
            )
        ).mappings():
            cleaned = rebind_value(row["payload"], {server_id: [] for server_id in server_ids})
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

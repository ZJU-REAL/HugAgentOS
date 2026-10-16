"""Remove the retired Batch feature from existing databases.

Used by Alembic and SQLite startup reconciliation. Stop old application
workers and take a database backup before calling this data retirement.
Ordinary conversations, uploaded/generated files, plans, automations and
workflow jobs remain intact. Deleted execution data needs a backup to restore.
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

_FLAGS = {"batch_chat", "batch_mode_active", "disable_batch_plan",
          "batchChat", "batchModeActive", "batch_runner", "batch_plan"}
_CONFIG_COLUMNS = {
    "users_shadow": ("metadata",),
    "teams": ("default_permissions",),
    "roles": ("permissions",),
    "chat_sessions": ("metadata",),
    "chat_messages": ("tool_calls",),
    "admin_skills": ("allowed_tools",),
    "chat_runs": ("request_payload", "recovery_snapshot"),
    "user_agents": ("mcp_server_ids", "extra_config"),
    "scheduled_tasks": ("enabled_mcp_ids",),
    "plans": ("metadata",),
    "plan_steps": ("expected_tools",),
    "projects": ("metadata",),
}
_CONTENT_IDS = {"app_config", "page_config", "docs_capabilities"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _FLAGS}
    if isinstance(value, list):
        return [
            _clean(item) for item in value
            if item not in ("batch_runner", "batch_plan")
            and not (isinstance(item, dict)
                     and (item.get("id", item.get("server_id")) == "batch_runner"
                          or item.get("tool_name", item.get("name")) == "batch_plan"))
        ]
    return value


def retire_batch_execution(bind: Connection) -> dict[str, int]:
    """Retire only Batch-owned records, in the caller's transaction."""
    metadata = sa.MetaData()
    names = set(sa.inspect(bind).get_table_names())
    report: dict[str, int] = {}

    def table(name: str):
        if name not in names:
            return None
        return sa.Table(name, metadata, autoload_with=bind)

    def delete_where(name: str, column: str, values: tuple[str, ...]):
        target = table(name)
        if target is not None and column in target.c:
            report[name] = report.get(name, 0) + bind.execute(
                target.delete().where(target.c[column].in_(values))
            ).rowcount

    # Remove child execution state explicitly as well as via FK cascades.
    runs = table("chat_runs")
    batch_runs: list[str] = []
    if runs is not None:
        for row in bind.execute(sa.select(runs.c.run_id, runs.c.request_payload)):
            payload = row.request_payload
            if isinstance(payload, dict) and payload.get("kind") == "batch_item":
                batch_runs.append(row.run_id)
    if batch_runs:
        ledger = table("tool_effect_ledger")
        effect_ids = []
        if ledger is not None:
            effect_ids = list(bind.execute(sa.select(ledger.c.effect_id).where(
                ledger.c.run_id.in_(batch_runs)
            )).scalars())
        if effect_ids:
            delete_where("tool_call_logs", "effect_id", tuple(effect_ids))
            delete_where("tool_effect_receipts", "effect_id", tuple(effect_ids))
        for name in ("tool_effect_leases", "tool_effect_ledger",
                     "chat_run_operations", "chat_steer_queue",
                     "harness_usage_cursors", "harness_usage_attempts",
                     "harness_event_cursors", "harness_event_log"):
            delete_where(name, "run_id", tuple(batch_runs))
        delete_where("chat_runs", "run_id", tuple(batch_runs))

    delete_where("tool_call_logs", "mcp_server", ("batch_runner",))
    delete_where("tool_call_logs", "tool_name", ("batch_plan",))
    delete_where("tool_effect_leases", "effect_id", tuple(bind.execute(
        sa.select(table("tool_effect_ledger").c.effect_id).where(
            table("tool_effect_ledger").c.tool_name == "batch_plan"
        )
    ).scalars()) if "tool_effect_ledger" in names else ())
    delete_where("tool_effect_ledger", "tool_name", ("batch_plan",))
    delete_where("tool_effect_receipts", "tool_name", ("batch_plan",))
    delete_where("catalog_overrides", "item_id", ("batch_runner",))
    delete_where("admin_mcp_servers", "server_id", ("batch_runner",))

    for name, columns in _CONFIG_COLUMNS.items():
        target = table(name)
        if target is None:
            continue
        keys = list(target.primary_key.columns)
        for column in columns:
            if column not in target.c:
                continue
            for row in bind.execute(sa.select(*keys, target.c[column])).mappings():
                original = row[column]
                if not isinstance(original, (dict, list)):
                    continue
                cleaned = _clean(original)
                if cleaned != original:
                    bind.execute(target.update().where(sa.and_(*[
                        key == row[key.name] for key in keys
                    ])).values({column: cleaned}))
                    report[name + "." + column] = report.get(name + "." + column, 0) + 1

    blocks = table("content_blocks")
    if blocks is not None:
        for row in bind.execute(sa.select(blocks.c.id, blocks.c.payload).where(
            blocks.c.id.in_(_CONTENT_IDS)
        )):
            cleaned = _clean(row.payload)
            if cleaned != row.payload:
                bind.execute(blocks.update().where(blocks.c.id == row.id)
                             .values(payload=cleaned))
                report["content_blocks"] = report.get("content_blocks", 0) + 1

    if "batch_plans" in names:
        target = table("batch_plans")
        report["batch_plans"] = bind.execute(sa.select(sa.func.count()).select_from(target)).scalar_one()
        target.drop(bind)
    return report

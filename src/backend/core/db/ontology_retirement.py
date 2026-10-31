"""Remove domain ontology governance data from a community database.

Used by the CE Alembic chain and by local SQLite startup, which has no Alembic.
Conversations keep their text; only ontology packs, reviews, settings and
bindings are deleted. Restoring them needs a backup taken before upgrading.
"""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

# Children before parents so the drops work without relying on FK cascades.
ONTOLOGY_TABLES = (
    "ontology_enforcement_events",
    "ontology_review_runs",
    "ontology_drafts",
    "ontology_pack_versions",
    "ontology_packs",
)
_USER_KEYS = ("ontology_enabled", "ontology_pack_ids", "can_use_ontology_validation")


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _strip_keys(bind: Connection, table: sa.Table, key: str, column: str, names: tuple[str, ...]):
    changed = 0
    for row in bind.execute(sa.select(table.c[key], table.c[column])):
        value = _json(row[1])
        if not isinstance(value, dict) or not any(name in value for name in names):
            continue
        cleaned = {k: v for k, v in value.items() if k not in names}
        # Reflected SQLite JSON columns are plain text; write back the stored form.
        stored = json.dumps(cleaned, ensure_ascii=False) if isinstance(row[1], str) else cleaned
        bind.execute(table.update().where(table.c[key] == row[0]).values({column: stored}))
        changed += 1
    return changed


def has_ontology_data(bind: Connection) -> bool:
    return bool(set(ONTOLOGY_TABLES) & set(sa.inspect(bind).get_table_names()))


def retire_ontology(bind: Connection) -> dict[str, int]:
    """Delete ontology-owned rows and settings in the caller's transaction."""
    names = set(sa.inspect(bind).get_table_names())
    metadata = sa.MetaData()
    report: dict[str, int] = {}

    def table(name: str):
        return sa.Table(name, metadata, autoload_with=bind) if name in names else None

    configs = table("system_configs")
    if configs is not None:
        report["system_configs"] = bind.execute(
            configs.delete().where(configs.c.config_key.like("ontology.%"))
        ).rowcount
    users = table("users_shadow")
    if users is not None and "metadata" in users.c:
        report["users_shadow"] = _strip_keys(bind, users, "user_id", "metadata", _USER_KEYS)
    agents = table("user_agents")
    if agents is not None and "extra_config" in agents.c:
        report["user_agents"] = _strip_keys(bind, agents, "agent_id", "extra_config", ("ontology_tags",))
    for name in ONTOLOGY_TABLES:
        if name in names:
            bind.execute(sa.text(f'DROP TABLE "{name}"'))
            report[name] = 1
    return report

"""一次性数据订正：把误存进用户数据里的内部文本清掉。

这里的函数由 alembic 迁移调用（主仓链与 CE overlay 链各有一条），逻辑只写一份。
"""

from __future__ import annotations

import sqlalchemy as sa

# 站点会话的建站 / 编辑规则曾由前端拼在用户消息尾部一起上行，于是原样落进
# ``chat_messages.content``。拼接格式固定：两个换行 + 下面两个开头之一，方括号块收在消息末尾。
_SITE_HINT_MARKERS = (
    "\n\n[系统提示：这是「站点建站」会话。",
    "\n\n[系统提示：这是「站点编辑」会话。",
)


def strip_site_mode_hint_from_user_messages(conn: sa.engine.Connection) -> int:
    """删掉历史用户消息尾部残留的站点规则，返回订正条数。

    规则由站点插件的 site-builder 技能提供，留在消息里
    对模型没有用处，只会在刷新页面时显示成用户自己说过的话。只截掉规则那一段，
    用户自己打的字原样保留。
    """
    rows = conn.execute(
        sa.text(
            "SELECT message_id, content FROM chat_messages "
            "WHERE role = 'user' AND content LIKE :pattern"
        ),
        {"pattern": "%[系统提示：这是「站点%会话。%"},
    ).fetchall()

    repaired = 0
    for message_id, content in rows:
        text = content or ""
        offsets = [text.index(m) for m in _SITE_HINT_MARKERS if m in text]
        if not offsets:
            continue
        cut = min(offsets)
        # cut == 0 意味着整条消息只有规则、没有用户原话，那不是拼接出来的消息，不动它。
        if cut == 0 or not text.rstrip().endswith("]"):
            continue
        conn.execute(
            sa.text("UPDATE chat_messages SET content = :content WHERE message_id = :mid"),
            {"content": text[:cut].rstrip(), "mid": message_id},
        )
        repaired += 1
    return repaired


def remove_desktop_site_prompt_parts(conn: sa.engine.Connection) -> int:
    """Retire site-only parts once; preserve other versions and custom project text."""
    from copy import deepcopy

    blocks = sa.table(
        "content_blocks", sa.column("id", sa.String), sa.column("payload", sa.JSON)
    )
    payload = conn.execute(
        sa.select(blocks.c.payload).where(blocks.c.id == "prompt_versions")
    ).scalar_one_or_none()
    if not isinstance(payload, dict):
        return 0
    updated = deepcopy(payload)
    retired = {"site_mode", "site_records", "site_lookup_failed"}
    # Exact legacy stock paragraph only; never rewrite administrator prose.
    legacy_project_line = (
        "建站在本地项目真实目录下的 sites/<站点名>/ 编写源码。"
        "编辑已有站点先读取当前站点的 source_dir 并原地修改；"
        "构建后 publish_site 的 src_dir 指向构建产物、source_dir 指向项目源码。"
    )
    legacy_project_lines = {
        legacy_project_line,
        "越出授权目录的操作与危险命令受本机权限策略约束；文件工具修改前的快照可用于回滚。",
    }
    repaired = 0
    for version in updated.get("versions", []):
        if version.get("kind") != "desktop":
            continue
        original = deepcopy(version.get("parts", []))
        parts = [part for part in original if part.get("part_id") not in retired]
        for part in parts:
            if part.get("part_id") != "project":
                continue
            content = part.get("content", "")
            part["content"] = "".join(
                line for line in content.splitlines(keepends=True)
                if line.strip() not in legacy_project_lines
            )
        if parts != version.get("parts", []):
            version["parts"] = parts
            repaired += 1
    if repaired:
        conn.execute(
            blocks.update().where(blocks.c.id == "prompt_versions").values(payload=updated)
        )
    return repaired


# Where capability bits are stored: (table, key column, JSON column, grant-only).
# Roles store granted bits only (``True``); users and teams store explicit ``True``/``False``.
_CAPABILITY_STORES = (
    ("users_shadow", "user_id", "metadata", False),
)


def copy_permission_bit(conn: sa.engine.Connection, source: str, target_bit: str) -> int:
    """Seed a newly split capability bit from the bit it was split from (see ``copy_permission_bits``)."""
    return copy_permission_bits(conn, ((source, target_bit),))


def copy_permission_bits(conn: sa.engine.Connection, pairs) -> int:
    """Seed newly split capability bits from the bits they were split from.

    For each ``(source, target_bit)`` pair: wherever ``source`` was configured (personal,
    team or role) and ``target_bit`` is not yet set, ``target_bit`` gets the same value, so
    nobody gains or loses access on upgrade. Each table is read once for all pairs. Missing
    tables (CE has no teams or roles) are skipped. Returns rows changed.
    """
    import json

    tables = set(sa.inspect(conn).get_table_names())
    changed = 0
    for table, key, column, grant_only in _CAPABILITY_STORES:
        if table not in tables:
            continue
        target = sa.table(table, sa.column(key), sa.column(column, sa.JSON))
        for row_key, raw in conn.execute(sa.select(target.c[key], target.c[column])).fetchall():
            value = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(value, dict):
                continue
            added = {}
            for source, target_bit in pairs:
                bit = value.get(source)
                if target_bit in value or not isinstance(bit, bool) or (grant_only and not bit):
                    continue
                added[target_bit] = bit
            if added:
                conn.execute(target.update().where(target.c[key] == row_key).values({column: {**value, **added}}))
                changed += 1
    return changed


def copy_lab_permission_to_sites(conn: sa.engine.Connection) -> int:
    """Seed the new ``sites_enabled`` bit from ``lab_enabled`` wherever lab was configured."""
    return copy_permission_bit(conn, "lab_enabled", "sites_enabled")


MARKET_PERMISSION_SPLIT = (("can_add_skill", "can_install_market_skill"), ("can_add_mcp", "can_install_market_mcp"))
PLUGIN_AGENT_MARKET_SPLIT = (
    ("can_import_plugin", "can_install_market_plugin"),
    ("can_add_agent", "can_install_market_agent"),
)
# One marker per migration, so a database that already ran the first copy still runs the second.
_MARKET_SPLIT_STEPS = (
    ("data-repair:mktperm01", MARKET_PERMISSION_SPLIT),
    ("data-repair:mktperm02", PLUGIN_AGENT_MARKET_SPLIT),
)


def copy_market_permissions_once(conn: sa.engine.Connection) -> bool:
    """SQLite twin of migrations ``mktperm01`` and ``mktperm02`` (SQLite databases skip alembic).

    Each copy runs exactly once, recorded by a ``content_blocks`` marker: copying on every
    start would undo an administrator who later sets a market bit back to "follow".
    Returns True when any copy ran.
    """
    from datetime import datetime, timezone

    tables = set(sa.inspect(conn).get_table_names())
    if "content_blocks" not in tables or "users_shadow" not in tables:
        return False
    blocks = sa.table(
        "content_blocks", sa.column("id"), sa.column("payload", sa.JSON),
        sa.column("updated_at"), sa.column("updated_by"),
    )
    ran = False
    for marker, pairs in _MARKET_SPLIT_STEPS:
        if conn.execute(sa.select(blocks.c.id).where(blocks.c.id == marker)).first():
            continue
        copy_permission_bits(conn, pairs)
        # Two processes starting together may both get here; the copy is idempotent, so the
        # loser of the marker insert just carries on instead of failing startup.
        try:
            with conn.begin_nested():
                conn.execute(blocks.insert().values(
                    id=marker, payload={"done": True},
                    updated_at=datetime.now(timezone.utc), updated_by="data_repair",
                ))
        except sa.exc.IntegrityError:
            continue
        ran = True
    return ran

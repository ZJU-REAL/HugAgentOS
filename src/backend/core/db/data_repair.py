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

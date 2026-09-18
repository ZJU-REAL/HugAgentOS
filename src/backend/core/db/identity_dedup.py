"""合并同一个用户中心身份下重复的影子用户。

``users_shadow`` 过去只有一条普通索引，没有唯一约束，而取用户走的是"先查后建"。
并发下几个请求会同时查不到、各建一行——桌面壳登录后一次性打出的那批桥接请求就足以
造出重复身份。会话、生成物、记忆等全部挂在 ``user_id`` 上，于是同一个人可能在不同
时刻被认到不同的行，看到的历史也就跟着变，表现成"数据被清空了"。

这里把重复行合并掉，为唯一约束扫清障碍：保留最早建的那一行（与查询排序一致，合并
前后认到的都是同一行），把其余行的引用整体改指过去。引用是按库里**实际存在**的
``user_id`` 列扫出来的，不写死表名，新表自动被覆盖。

**任何一行被删之前都会先落盘备份**：备份在整段合并的最后、提交之前写；写不成功就
抛错，整个事务回滚，宁可留着重复也不无备份地动用户数据。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from core.db.schema_reconcile import connection_scope

logger = logging.getLogger(__name__)

UNIQUE_INDEX_NAME = "uq_users_shadow_user_center_id"


def default_backup_dir() -> Path:
    """本机数据根下的备份目录（``HUGAGENT_HOME`` 由桌面壳注入）。"""
    from core.config.runtime_env import local_data_dir

    return local_data_dir() / "backups"


def _expanding(sql: str) -> Any:
    return text(sql).bindparams(bindparam("ids", expanding=True))


def _duplicate_groups(connection: Connection) -> Dict[str, List[str]]:
    """``{user_center_id: [user_id, ...]}``，每组按查询用的同一套排序排列（首个即保留行）。"""
    rows = connection.execute(
        text(
            "SELECT user_center_id, user_id FROM users_shadow "
            "WHERE user_center_id IS NOT NULL AND user_center_id <> '' "
            "AND user_center_id IN ("
            "    SELECT user_center_id FROM users_shadow "
            "    WHERE user_center_id IS NOT NULL AND user_center_id <> '' "
            "    GROUP BY user_center_id HAVING COUNT(*) > 1"
            ") "
            "ORDER BY user_center_id, created_at, user_id"
        )
    ).mappings()
    groups: Dict[str, List[str]] = {}
    for row in rows:
        groups.setdefault(str(row["user_center_id"]), []).append(str(row["user_id"]))
    return groups


def _tables_with_user_id(connection: Connection) -> List[str]:
    """库里所有带 ``user_id`` 列的表。

    按列名而不是外键来找：审计日志、记忆、可观测性等表指的是同一个人却没有声明外键，
    只认外键会把它们漏掉，重复行删掉后这些记录就成了孤儿。
    """
    inspector = inspect(connection)
    columns_by_table = inspector.get_multi_columns()
    names = set()
    for key, columns in columns_by_table.items():
        table_name = key[1] if isinstance(key, tuple) else key
        if table_name == "users_shadow":
            continue
        if any(column["name"] == "user_id" for column in columns):
            names.add(table_name)
    return sorted(names)


def _rows_for(connection: Connection, table_name: str, user_ids: List[str]) -> List[Dict[str, Any]]:
    quoted = connection.dialect.identifier_preparer.quote(table_name)
    result = connection.execute(
        _expanding(f"SELECT * FROM {quoted} WHERE user_id IN :ids"), {"ids": user_ids}
    ).mappings()
    return [dict(row) for row in result]


def _write_backup(backup_dir: Path, payload: Dict[str, Any]) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = backup_dir / f"users_shadow_merge_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return str(path)


def merge_duplicate_user_shadows(
    bind: Engine | Connection,
    *,
    backup_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """合并重复影子用户。唯一约束已在位、或本来就没有重复时，不写任何东西。

    备份写不成功会抛错，让调用方的事务回滚——删用户数据必须有退路。
    """
    report: Dict[str, Any] = {
        "groups": 0,
        "removed_user_ids": [],
        "repointed": {},
        "dropped": {},
        "backup_path": None,
    }
    backup_dir = backup_dir or default_backup_dir()
    with connection_scope(bind) as connection:
        inspector = inspect(connection)
        if not inspector.has_table("users_shadow"):
            return report
        # 唯一索引一旦在位，重复就不可能再出现——省掉每次启动的全表分组扫描。
        if any(
            index.get("name") == UNIQUE_INDEX_NAME
            for index in inspector.get_indexes("users_shadow")
        ):
            return report

        groups = _duplicate_groups(connection)
        if not groups:
            return report

        losers_of: Dict[str, List[str]] = {}
        losers: List[str] = []
        for user_ids in groups.values():
            losers_of[user_ids[0]] = user_ids[1:]
            losers.extend(user_ids[1:])
        report["groups"] = len(groups)

        backup: Dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "removed_rows": {"users_shadow": _rows_for(connection, "users_shadow", losers)},
        }

        for table_name in _tables_with_user_id(connection):
            quoted = connection.dialect.identifier_preparer.quote(table_name)
            for winner, group_losers in losers_of.items():
                try:
                    with connection.begin_nested():
                        result = connection.execute(
                            _expanding(
                                f"UPDATE {quoted} SET user_id = :winner WHERE user_id IN :ids"
                            ),
                            {"winner": winner, "ids": group_losers},
                        )
                    if result.rowcount:
                        report["repointed"][table_name] = (
                            report["repointed"].get(table_name, 0) + result.rowcount
                        )
                except IntegrityError:
                    # 改指撞上了唯一键——保留行已经有等价记录（每用户一行的表，
                    # 比如本地账号、项目成员）。丢掉重复那份，保留行的才是在用的；
                    # 删之前先把整行内容收进备份。
                    doomed = _rows_for(connection, table_name, group_losers)
                    if not doomed:
                        continue
                    backup["removed_rows"].setdefault(table_name, []).extend(doomed)
                    with connection.begin_nested():
                        connection.execute(
                            _expanding(f"DELETE FROM {quoted} WHERE user_id IN :ids"),
                            {"ids": group_losers},
                        )
                    report["dropped"][table_name] = (
                        report["dropped"].get(table_name, 0) + len(doomed)
                    )

        connection.execute(
            _expanding("DELETE FROM users_shadow WHERE user_id IN :ids"), {"ids": losers}
        )
        report["removed_user_ids"] = sorted(losers)
        # 备份放在最后写：此时才知道所有要删的行。写失败即抛错，事务整体回滚。
        report["backup_path"] = _write_backup(backup_dir, backup)

    logger.warning(
        "Merged duplicate user shadows: groups=%s removed=%s repointed=%s dropped=%s backup=%s",
        report["groups"],
        report["removed_user_ids"],
        report["repointed"],
        report["dropped"],
        report["backup_path"],
    )
    return report

#!/usr/bin/env python3
"""一次性对账：把沙箱镜像目录与「我的空间」拉回一致。

背景见 ``core/llm/tools/myspace_mirror.py``。修复上线后每次 bash、每次打开「我的空间」
都会实时对账，本脚本负责清掉修复之前积压的两类历史欠账：

- **该显示没显示**：沙箱写在 ``/myspace`` 下的文件没有 artifact 记录，用户在界面上看不见，
  新会话却每次都挂得到 → 登记进「我的空间」；
- **该删没删**：用户在界面上删掉的文件，镜像里的副本还留着，新沙箱照样看得见 → 从镜像清掉；
  整个文件夹被删、里面的文件又从没登记过的那批算「残留」，**默认只统计不删** ——
  它们在对象存储里没有副本，删了找不回来，要清得显式加 ``--prune-stale``。

改动了用户已有文件的那一类不在这里处理 —— 那要过写入确认门，交给 bash 工具在对话里问。

用法（在 backend 容器里跑）：

    python scripts/reconcile_myspace_mirror.py --all --dry-run   # 先看规模
    python scripts/reconcile_myspace_mirror.py --all             # 真正执行
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _user_ids(explicit: list[str], scan_all: bool) -> list[str]:
    if explicit:
        return explicit
    if not scan_all:
        return []
    from core.config.settings import settings

    root = settings.storage.root / "myspace_cache"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def main() -> int:
    ap = argparse.ArgumentParser(description="镜像目录与「我的空间」一次性对账")
    ap.add_argument("--user", action="append", default=[], help="用户 id，可重复")
    ap.add_argument("--all", action="store_true", help="处理 myspace_cache 下的所有用户")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库不删文件")
    ap.add_argument("--verbose", action="store_true", help="打印待登记文件的路径")
    ap.add_argument(
        "--prune-stale", action="store_true",
        help="删掉镜像里的残留副本（用户已删的文件 / 已删文件夹里的东西）。"
             "注意：这些文件多半从没登记过，对象存储里没有副本，删了找不回来。",
    )
    args = ap.parse_args()

    users = _user_ids(args.user, args.all)
    if not users:
        ap.error("至少要给 --user <uid> 或 --all")

    from core.llm.tools import myspace_mirror as mm

    for uid in users:
        changes = mm.collect_mirror_changes(user_id=uid)
        print(
            f"[{uid}] 镜像 {changes.scanned} 个文件："
            f"待登记 {len(changes.new)}、改动待确认 {len(changes.modified)}、"
            f"已是最新 {changes.skipped_current}、用户已删残留 {len(changes.stale)}、"
            f"超限跳过 {changes.skipped_too_large}"
        )
        if args.verbose:
            for entry in changes.new[:200]:
                print(f"    + {entry.logical_path}")
            if len(changes.new) > 200:
                print(f"    …… 还有 {len(changes.new) - 200} 个")
        if args.dry_run:
            continue
        # 先清残留（正向），再登记新文件：顺序反了会把刚清掉的又登记回去
        mm.reset_pull_cursor(uid)
        pull = mm.pull_myspace_updates(user_id=uid)
        refs = mm.register_new_files(user_id=uid)
        pruned = (
            mm.prune_stale(user_id=uid, entries=changes.stale) if args.prune_stale else 0
        )
        print(
            f"[{uid}] 执行完成：登记 {len(refs)}，镜像物化 {pull.materialized}，"
            f"清理已删文件 {pull.removed}，清理残留 {pruned}，失败 {pull.failed}"
        )

    if args.dry_run:
        print("（dry-run，未写入任何内容）")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())

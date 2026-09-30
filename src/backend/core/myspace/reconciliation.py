"""Explicit maintenance scans; normal writes are handled by the registry."""
from __future__ import annotations
import os
import logging
from typing import Optional, Iterator, Any
from pathlib import Path
from core.myspace import mirror as mm
logger = logging.getLogger(__name__)
def _mirror_root(user_id: str) -> Optional[Path]:
    from core.sandbox._common import myspace_cache_dir

    root = myspace_cache_dir(user_id)
    return root if root.is_dir() else None


def iter_mirror_files(user_id: str) -> Iterator[mm.MirrorEntry]:
    """遍历镜像目录里的文件。

    单次改动由文件系统事件按路径处理（:func:`classify_claimed`），走到这里的只有"把整个目录
    和账本对一遍"那一种场景：人工对账脚本。
    """
    root = _mirror_root(user_id)
    if root is None:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in mm.SKIP_DIR_NAMES]
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            yield mm.MirrorEntry(rel=rel, path=fp, size=st.st_size, mtime=st.st_mtime)


def collect_mirror_changes(
    *,
    user_id: str,
    max_bytes: Optional[int] = None,
) -> mm.MirrorChanges:
    """扫描镜像目录，把待处理的文件按 :func:`mm._classify_entry` 的判定归类。

    **只判断，不写任何东西**。给人工对账脚本用；日常的单次改动由 :func:`classify_claimed`
    按路径判定，不必扫目录。
    """
    out = mm.MirrorChanges()
    if not user_id:
        return out
    if max_bytes is None:
        from core.config.settings import settings

        max_bytes = settings.sandbox.artifact_max_bytes
    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过对账: %s", exc)
        return out

    db = SessionLocal()
    chain_memo: dict[tuple[str, ...], mm._Chain] = {}
    try:
        for entry in iter_mirror_files(user_id):
            out.scanned += 1
            verdict = mm._classify_entry(
                db, user_id, entry, max_bytes=max_bytes, chain_memo=chain_memo
            )
            if verdict == mm.VERDICT_TOO_LARGE:
                out.skipped_too_large += 1
            elif verdict == mm.VERDICT_STALE:
                out.stale.append(entry)
            elif verdict == mm.VERDICT_CURRENT:
                out.skipped_current += 1
            elif verdict == mm.VERDICT_NEW:
                out.new.append(entry)
            else:
                out.modified.append(entry)
    finally:
        db.close()
    return out


def prune_stale(*, user_id: str, entries: list[mm.MirrorEntry]) -> int:
    """删掉镜像里的残留副本（用户已删的文件 / 已删文件夹里的东西）。

    **只由人显式触发**（``scripts/reconcile_myspace_mirror.py --prune-stale``），不挂在
    任何自动路径上：这些文件多半从没登记过，对象存储里没有副本，删掉就找不回来了。
    """
    removed = 0
    for entry in entries:
        try:
            entry.path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("[myspace-mirror] 清理残留失败 %s: %s", entry.rel, exc)
    if removed:
        logger.info("[myspace-mirror] 清理残留 user=%s 共 %d 个", user_id, removed)
    return removed

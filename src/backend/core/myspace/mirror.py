"""「我的空间」与沙箱镜像目录之间的判定与搬运。

沙箱里的 ``/workspace/myspace/{uid}`` 和后端的 ``storage/myspace_cache/{uid}`` 是同一份
磁盘目录 —— 写在这个路径下的文件**就是**用户「我的空间」里的文件，只差一条 artifact
登记记录。缺了那条记录的文件界面上看不见也删不掉，而每个新建沙箱都会把这份目录挂进来，
于是成了跨会话互相串文件的根源。

本模块只回答"某个文件相对账本是什么状态"并执行搬运，**不决定什么时候做**；触发全部交给
:mod:`core.space_sync.personal`，由文件系统事件驱动。判定基准是 **artifact 记录**，不是镜像
缓存自身：两者在 bind mount 下是同一份文件，拿它和自己比恒等为真。

两个方向：

- :func:`classify_claimed` / :func:`collect_mirror_changes` 判断磁盘上的文件相对账本的状态
  （**只判断、不写**）。「用户已删的残留」既不登记也不自动删 —— 登记会把用户的清理
  撤销，自动删又可能丢掉对象存储里没有副本的内容，交给
  ``scripts/reconcile_myspace_mirror.py --prune-stale`` 由人决定。
- :func:`pull_myspace_updates` 我的空间 → 镜像目录：界面上传/改动落进镜像（bind mount
  下即刻对沙箱可见），界面上删掉的文件同步从镜像移除。

本模块只处理个人空间；团队/项目 scope 有各自的缓存目录，走原有路径。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.llm.tools import myspace_vfs as _ms
from core.services.artifact_edition import personal_artifact_predicates
from sqlalchemy import or_

logger = logging.getLogger(__name__)


# 同一次写入里「DB 提交」和「文件落盘」总有先后差，两边时间戳留 2s 容差再比先后。
_CLOCK_SLACK_S = 2.0

# 镜像目录里不算用户文件的目录名：运行期垃圾。遍历和文件事件两边共用这一份。
SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".ipynb_checkpoints"})


@dataclass
class MirrorEntry:
    """镜像目录里的一个文件。``rel`` 相对用户根目录，同时就是它的逻辑路径。"""

    rel: str
    path: Path
    size: int
    mtime: float

    @property
    def logical_path(self) -> str:
        return f"{_ms.MYSPACE_LOGICAL}/{self.rel}"


@dataclass
class MirrorChanges:
    """一次对账的分类结果。``new`` 直接登记；``modified`` 由调用方决定是否要确认。"""

    new: list[MirrorEntry] = field(default_factory=list)
    modified: list[MirrorEntry] = field(default_factory=list)
    # 用户已经删掉（文件本身或它所在的文件夹），镜像里却还留着的残留副本。既不登记也不
    # 自动删 —— 这类文件多半从没登记过，对象存储里没有副本，删了就真没了，交给人决定。
    stale: list[MirrorEntry] = field(default_factory=list)
    scanned: int = 0
    skipped_current: int = 0
    skipped_too_large: int = 0


@dataclass
class _Chain:
    """镜像路径对应的目录链解析结果。"""

    folder_id: Optional[str] = None
    exists: bool = False  # 每一级都能找到（含已被删除的目录）
    deleted_ts: Optional[float] = None  # 链上最近一次删除的时刻


def _resolve_chain(db: Any, user_id: str, names: list[str]) -> _Chain:
    """按名字链解析目录，**把已删除的目录也算进来**。

    用户在界面上删的往往是**整个文件夹**：文件夹标了删除，里面的文件在镜像目录里还原样
    躺着。若只按在册目录解析，这条链会解析失败、里面的文件被判成"从没登记过的新文件"，
    于是连文件夹一起重建回用户空间 —— 用户刚清理掉的东西第二天全回来了。所以必须认出
    "这条路径已经被删除"，并记下删除时刻，好区分"删除前的残留"和"删除之后又写的新内容"。
    """
    from core.db.models import UserFolder

    chain = _Chain(exists=True)
    parent: Optional[str] = None
    for name in names:
        q = db.query(UserFolder).filter(UserFolder.user_id == user_id, UserFolder.name == name)
        q = (
            q.filter(UserFolder.parent_folder_id.is_(None))
            if parent is None
            else q.filter(UserFolder.parent_folder_id == parent)
        )
        row = q.order_by(UserFolder.created_at.desc()).first()
        if row is None:
            return _Chain(folder_id=parent, exists=False, deleted_ts=chain.deleted_ts)
        deleted = _ts(row.deleted_at)
        if deleted is not None:
            chain.deleted_ts = (
                deleted if chain.deleted_ts is None else max(chain.deleted_ts, deleted)
            )
        parent = row.folder_id
    chain.folder_id = parent
    return chain


def _artifact_in(db: Any, user_id: str, folder_id: Optional[str], filename: str) -> Any:
    """在指定目录里按文件名定位 artifact，**含已被用户删除的那条**。

    删除记录也要找出来：用户删掉的文件镜像里往往还留着副本，只查在册记录会把它判成
    "从没登记过"，于是又登记一遍 —— 等于把用户的删除撤销。
    """
    from core.db.models import Artifact

    q = db.query(Artifact).filter(
        Artifact.user_id == user_id,
        Artifact.filename == filename,
        *personal_artifact_predicates(Artifact),
    )
    q = (
        q.filter(Artifact.user_folder_id.is_(None))
        if folder_id is None
        else q.filter(Artifact.user_folder_id == folder_id)
    )
    return q.order_by(Artifact.created_at.desc()).first()


def _artifacts_in(
    db: Any, user_id: str, folder_id: Optional[str], filenames: list[str]
) -> dict[str, Any]:
    """一个目录里这一批文件名各自的 artifact（含已删除的那条），一次查询取回。

    逐个文件查会让"一条命令往同一个目录落几百个文件"变成几百次 SELECT；判据不变，只是
    把 N 次等值查询合成一次 ``IN``，同名多条取最新的那条（与 :func:`_artifact_in` 一致）。
    """
    from core.db.models import Artifact

    if not filenames:
        return {}
    q = db.query(Artifact).filter(
        Artifact.user_id == user_id,
        Artifact.filename.in_(filenames),
        *personal_artifact_predicates(Artifact),
    )
    q = (
        q.filter(Artifact.user_folder_id.is_(None))
        if folder_id is None
        else q.filter(Artifact.user_folder_id == folder_id)
    )
    out: dict[str, Any] = {}
    for art in q.order_by(Artifact.created_at.desc()).all():
        out.setdefault(str(art.filename), art)  # 已按时间倒序，第一条就是最新的
    return out


def _artifact_for_rel(db: Any, user_id: str, rel: str) -> Any:
    """按镜像相对路径定位 artifact（目录链不存在时返回 None）。"""
    folder_names, filename = _ms.split_rel(rel)
    if not filename:
        return None
    chain = _resolve_chain(db, user_id, folder_names)
    if not chain.exists:
        return None
    return _artifact_in(db, user_id, chain.folder_id, filename)


def _ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return float(value.timestamp())


def _artifact_ts(art: Any) -> Optional[float]:
    if art is None:
        return None
    return _ts(art.updated_at or art.created_at)


def _artifact_is_current(art: Any, entry: MirrorEntry) -> bool:
    """artifact 是否已经反映了镜像里的这份内容 —— 判据只有一个：谁更新。

    只有镜像文件严格比 artifact 新，才说明这份改动还没登记回去。**不能拿"大小不一样"
    当依据**：用户在界面上重新上传同名文件时只改 artifact、不碰镜像，此时两边大小往往
    也不同，若据此把镜像里的旧副本推上去，就用旧内容盖掉了用户刚传的新版本。方向相反的
    差异由 :func:`pull_myspace_updates` 把新内容拉下来。
    """
    from core.space_sync.content import current_snapshot

    current = current_snapshot(art, entry)
    if current is not None:
        return current
    ts = _artifact_ts(art)
    return ts is not None and ts + 0.000001 >= entry.mtime


# 一个镜像文件相对账本的状态。
VERDICT_NEW = "new"  # 账本里没有这条记录，登记之后用户才看得见
VERDICT_MODIFIED = "modified"  # 账本里有，磁盘上这份更新 —— 改的是用户已有的文件
VERDICT_CURRENT = "current"  # 账本已经反映了这份内容，无事可做
VERDICT_STALE = "stale"  # 用户已经删了它（或它所在的文件夹），磁盘上是残留副本
VERDICT_TOO_LARGE = "too_large"  # 超过单个生成物的大小上限，不登记


def mirror_entry(user_id: str, rel: str) -> Optional[MirrorEntry]:
    """镜像目录里这个相对路径的文件；不存在或不是普通文件时返回 ``None``。"""
    fp = _ms.myspace_cache_file(user_id, rel)
    try:
        st = fp.stat()
    except OSError:
        return None
    if not fp.is_file():
        return None
    return MirrorEntry(rel=rel, path=fp, size=st.st_size, mtime=st.st_mtime)


def _classify_entry(
    db: Any,
    user_id: str,
    entry: MirrorEntry,
    *,
    max_bytes: int,
    chain_memo: dict[tuple[str, ...], _Chain],
    artifact_memo: Optional[dict[tuple[Optional[str], str], Any]] = None,
) -> str:
    """这个镜像文件相对账本处于什么状态。两个 memo 让一批文件共用目录链解析与 artifact 查询。"""
    if entry.size > max_bytes:
        return VERDICT_TOO_LARGE
    folder_names, filename = _ms.split_rel(entry.rel)
    if not filename:
        return VERDICT_CURRENT
    memo_key = tuple(folder_names)
    chain = chain_memo.get(memo_key)
    if chain is None:
        chain = _resolve_chain(db, user_id, folder_names)
        chain_memo[memo_key] = chain
    if not chain.exists:
        art = None
    elif artifact_memo is not None:
        art = artifact_memo.get((chain.folder_id, filename))
    else:
        art = _artifact_in(db, user_id, chain.folder_id, filename)
    deleted_ts = _ts(getattr(art, "deleted_at", None)) if art is not None else None
    # 文件自己被删、或它所在的文件夹被删，镜像里的都只是残留副本 —— 登记它等于把用户的
    # 删除撤销（真实发生过：整个文件夹被删，里面 2000 多个文件还在镜像里）。反过来，
    # 删除之后沙箱又写了同名文件，那是新内容，按新文件处理。
    residue_ts = max((t for t in (deleted_ts, chain.deleted_ts) if t is not None), default=None)
    if residue_ts is not None and residue_ts + 0.000001 >= entry.mtime:
        return VERDICT_STALE
    if _artifact_is_current(art, entry):
        return VERDICT_CURRENT
    if art is None or deleted_ts is not None:
        return VERDICT_NEW
    return VERDICT_MODIFIED


def classify_claimed(*, user_id: str, entries: dict[str, MirrorEntry]) -> dict[str, str]:
    """一批已经认领下来的文件，各自相对账本是什么状态。

    整批共用一个 DB 会话、一份目录链缓存和一次按目录的 artifact 批量查询：一条命令往同一
    个目录里落几百个文件是常事，逐个开会话、逐个解目录链、逐个 SELECT，开销与文件数成
    正比地白涨。
    """
    out: dict[str, str] = {}
    if not user_id or not entries:
        return out

    from core.config.settings import settings

    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过判定: %s", exc)
        return {rel: VERDICT_CURRENT for rel in entries}

    max_bytes = settings.sandbox.artifact_max_bytes
    db = SessionLocal()
    chain_memo: dict[tuple[str, ...], _Chain] = {}
    try:
        # 先把目录链解出来，再按目录一次性取 artifact，最后才逐个判定。
        by_folder: dict[Optional[str], set[str]] = {}
        for rel in entries:
            folder_names, filename = _ms.split_rel(rel)
            if not filename:
                continue
            memo_key = tuple(folder_names)
            chain = chain_memo.get(memo_key)
            if chain is None:
                chain = _resolve_chain(db, user_id, folder_names)
                chain_memo[memo_key] = chain
            if chain.exists:
                by_folder.setdefault(chain.folder_id, set()).add(filename)
        artifact_memo: dict[tuple[Optional[str], str], Any] = {}
        for folder_id, names in by_folder.items():
            for name, art in _artifacts_in(db, user_id, folder_id, sorted(names)).items():
                artifact_memo[(folder_id, name)] = art
        for rel, entry in entries.items():
            out[rel] = _classify_entry(
                db,
                user_id,
                entry,
                max_bytes=max_bytes,
                chain_memo=chain_memo,
                artifact_memo=artifact_memo,
            )
    finally:
        db.close()
    return out


@dataclass
class DeleteTarget:
    """磁盘上消失的那个路径，在账本里对应着什么。"""

    registered: Optional["RegisteredFile"] = None  # 是个还在册的文件
    folder: bool = False  # 是个还在册的文件夹
    folder_id: Optional[str] = None
    folder_ts: Optional[float] = None

    @property
    def stamp(self) -> str:
        """认领这次删除用的标记 —— 同一条记录只该被删一次。"""
        return self.registered.artifact_id if self.registered else str(self.folder_id)

    @property
    def kind_label(self) -> str:
        return "文件" if self.registered else "文件夹"


def classify_deletes(*, user_id: str, rels: list[str]) -> dict[str, Optional[DeleteTarget]]:
    """一批消失的路径各自对应账本里的什么；没登记过的返回 ``None``。

    和 :func:`classify_claimed` 一样整批共用一个会话和目录链缓存 —— ``rm -rf`` 一个几百
    文件的目录会一次性送来几百条删除。
    """
    out: dict[str, Optional[DeleteTarget]] = {}
    if not user_id or not rels:
        return out
    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过删除判定: %s", exc)
        return {rel: None for rel in rels}

    db = SessionLocal()
    chain_memo: dict[tuple[str, ...], _Chain] = {}
    try:
        for rel in rels:
            out[rel] = _delete_target(db, user_id, rel, chain_memo)
    finally:
        db.close()
    return out


def _delete_target(
    db: Any, user_id: str, rel: str, chain_memo: dict[tuple[str, ...], _Chain]
) -> Optional[DeleteTarget]:
    """先按文件找，找不到再按文件夹找 —— ``rm -rf`` 只让路径消失，分不出删的是哪一种。"""
    folder_names, filename = _ms.split_rel(rel)
    if filename:
        memo_key = tuple(folder_names)
        chain = chain_memo.get(memo_key)
        if chain is None:
            chain = _resolve_chain(db, user_id, folder_names)
            chain_memo[memo_key] = chain
        if chain.exists:
            art = _artifact_in(db, user_id, chain.folder_id, filename)
            if art is not None and getattr(art, "deleted_at", None) is None:
                return DeleteTarget(registered=_registered_of(art))
    names = [n for n in rel.split("/") if n]
    if names:
        fr = _ms.resolve_folder_id(db, user_id, names, create=False)
        if fr.found and fr.folder_id:
            from core.db.models import UserFolder

            row = db.get(UserFolder, fr.folder_id)
            return DeleteTarget(
                folder=True, folder_id=fr.folder_id, folder_ts=_ts(row.updated_at or row.created_at)
            )
    return None


def register_entry(*, user_id: str, entry: MirrorEntry) -> Optional[dict]:
    """把镜像里的一个文件登记进「我的空间」（已存在则同 file_id 就地更新）。

    不挂会话：文件事件里只有路径、没有会话身份，见 :mod:`core.space_sync.personal` 模块说明。
    """
    from core.space_sync.content import register

    return register(user_id, entry)


def delete_registered(*, user_id: str, rel: str, target=None) -> bool:
    """把「我的空间」里对应的文件也删掉（软删，和界面上删除同一条路径）。"""
    if target is not None:
        from core.myspace.event_deletion import delete_observed

        return delete_observed(user_id=user_id, rel=rel, target=target)
    res = _ms.sync_delete(user_id, f"{_ms.MYSPACE_LOGICAL}/{rel}")
    if res.get("error"):
        logger.warning("[myspace-mirror] 同步删除失败 %s: %s", rel, res["error"])
        return False
    logger.info("[myspace-mirror] 同步删除 user=%s %s", user_id, rel)
    return True


def stamp_registered(user_id: str, rel: str, ts: Optional[float]) -> None:
    """把镜像文件的 mtime 对齐到账本里的登记时间。

    「磁盘上这份比账本新」是判断"这次改动还没登记"的唯一依据（见
    :func:`_artifact_is_current`）。后端自己往镜像目录写文件时如果留下当下的 mtime，
    这个依据就假了：文件系统监听会把后端刚写下去的内容当成沙箱写的新改动，转头再登记
    一次，用户还会莫名收到一次"是否允许覆盖"的确认。对齐时间戳之后就没有这回事，也
    不必另外维护一张"这是我自己写的"的登记表。
    """
    if ts is None:
        return
    try:
        os.utime(_ms.myspace_cache_file(user_id, rel), (ts, ts))
    except OSError as exc:  # noqa: BLE001 — 对不齐只会多一次无害的重复判定
        logger.warning("[myspace-mirror] 对齐 mtime 失败 %s: %s", rel, exc)


@dataclass
class RegisteredFile:
    """账本里登记着的一个文件（已软删的不算）。"""

    artifact_id: str
    storage_key: str
    registered_ts: Optional[float]


def _registered_of(art: Any) -> RegisteredFile:
    return RegisteredFile(
        artifact_id=str(art.artifact_id),
        storage_key=str(art.storage_key),
        registered_ts=_artifact_ts(art),
    )


def registered_file(user_id: str, rel: str) -> Optional[RegisteredFile]:
    """这个路径在「我的空间」里对应的文件；没登记过或已被删掉时返回 ``None``。"""
    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过查账: %s", exc)
        return None

    db = SessionLocal()
    try:
        art = _artifact_for_rel(db, user_id, rel)
        if art is None or getattr(art, "deleted_at", None) is not None:
            return None
        return _registered_of(art)
    finally:
        db.close()


def restore_from_registry(*, user_id: str, rel: str, reg: Optional[RegisteredFile] = None) -> bool:
    """把账本里的那一版内容写回镜像目录。

    用户否决了沙箱这次改写或删除时用它还原：文件在磁盘上早就被改/删了，确认门能做的
    不是"拦住"，而是"退回去"。还原之后时间戳对齐登记时间，两边重新一致。

    ``reg`` 给已经查过账的调用方，省掉一次重复查询。
    """
    if reg is None:
        reg = registered_file(user_id, rel)
    if reg is None:
        return False
    try:
        from core.storage import get_storage

        data = get_storage().download_bytes(reg.storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] 还原下载失败 %s: %s", rel, exc)
        return False
    _ms.mirror_to_cache(user_id, rel, data)
    stamp_registered(user_id, rel, reg.registered_ts)
    logger.info("[myspace-mirror] 已还原 user=%s %s", user_id, rel)
    return True

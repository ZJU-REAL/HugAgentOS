"""「我的空间」与沙箱镜像目录之间的实时对账。

开启 myspace bind mount 后，沙箱里的 ``/workspace/myspace/{uid}`` 和后端的
``storage/myspace_cache/{uid}`` 是同一份磁盘目录 —— 写在这个路径下的文件**就是**用户
「我的空间」里的文件，只差一条 artifact 登记记录。旧的回写逻辑拿「沙箱文件」和「镜像
缓存」比 md5、相同就跳过，在这种拓扑下恒等为真，于是从不回写：内容真实落在用户空间的
磁盘上，界面上却看不见也删不掉，而每个新建沙箱都会把这份目录挂进来，成了跨会话互相串
文件的根源。

本模块把判定基准从「镜像缓存」换成「artifact 记录」，提供两个方向：

- :func:`collect_mirror_changes` 分类镜像目录里待处理的改动（**只判断、不写**）：
  「新文件」直接登记展示；「改动了用户已有文件」要不要过写入确认门由调用方决定（bash
  工具过，HTTP 读时对账不过）；「用户已删的残留」（文件本身或它所在的文件夹被删）既不
  登记也不自动删 —— 登记会把用户的清理撤销，自动删又可能丢掉对象存储里没有副本的内容，
  交给 ``scripts/reconcile_myspace_mirror.py --prune-stale`` 由人决定。
- :func:`pull_myspace_updates` 我的空间 → 镜像目录：界面上传/改动落进镜像（bind mount
  下即刻对沙箱可见），界面上删掉的文件同步从镜像移除。

两侧都以 mtime / ``updated_at`` 为准做增量，没有变化时只有一次目录遍历和一次 DB 查询。
本模块只处理个人空间；团队/项目 scope 有各自的缓存目录，走原有路径。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.llm.tools import myspace_vfs as _ms
from core.services.artifact_edition import personal_artifact_predicates
from sqlalchemy import or_

logger = logging.getLogger(__name__)

# 同一进程内每个用户的对账水位（epoch 秒）。缺失表示本进程还没为该用户对过账，此时做一次
# 全量比对，之后转增量 —— 读时对账挂在会被前端轮询的列表接口上，全量扫 2000 个文件、逐个
# 查库是扛不住的。
_pull_cursor: dict[str, float] = {}
_read_cursor: dict[str, float] = {}

# 读时对账单轮最多登记多少个文件：登记要把内容传进对象存储，首次遇到几百 MB 存量时不能
# 让「我的空间」列表接口一直转圈。没登记完就不推进水位，下一次打开接着做。
_READ_REGISTER_BATCH = 50

# 同一次写入里「DB 提交」和「文件落盘」总有先后差，两边时间戳留 2s 容差再比先后。
_CLOCK_SLACK_S = 2.0

# 镜像目录里不参与对账的目录名：运行期垃圾，不是用户文件。
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".ipynb_checkpoints"})


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
    skipped_deleted: int = 0  # 用户在界面上删过，镜像里的是残留副本
    skipped_too_large: int = 0


@dataclass
class PullReport:
    materialized: int = 0
    removed: int = 0
    failed: int = 0


def _mirror_root(user_id: str) -> Optional[Path]:
    from core.sandbox._common import myspace_cache_dir

    root = myspace_cache_dir(user_id)
    return root if root.is_dir() else None


def iter_mirror_files(
    user_id: str,
    *,
    since_ts: Optional[float] = None,
    baseline: Optional[dict[str, tuple[float, int]]] = None,
) -> Iterator[MirrorEntry]:
    """遍历镜像目录里的文件。

    ``baseline``：命令执行前拍下的 ``{相对路径: (mtime, 大小)}`` 快照，只返回"快照里没有"
    或"mtime/大小变了"的文件。**这是首选的判定方式** —— 拿目录自己和自己比，不涉及任何
    时钟。之所以连大小一起比：mtime 的精度只到内核时钟 tick，同一 tick 内的改写不会体现
    在 mtime 上，大小能把这类改写捞回来。

    ``since_ts``：退而求其次的时间窗口，只在拿不到快照时使用。注意 Linux 写文件时记的
    mtime 取自内核的粗粒度时钟，实测会比 ``time.time()`` 慢几毫秒，所以窗口起点要往前
    让出 ``_CLOCK_SLACK_S``，否则命令刚写出来的文件会被判成"窗口之前的旧文件"而漏掉。
    """
    cutoff = None if since_ts is None else since_ts - _CLOCK_SLACK_S
    root = _mirror_root(user_id)
    if root is None:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            if baseline is not None:
                if baseline.get(rel) == (st.st_mtime, st.st_size):
                    continue
            elif cutoff is not None and st.st_mtime < cutoff:
                continue
            yield MirrorEntry(rel=rel, path=fp, size=st.st_size, mtime=st.st_mtime)


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
        q = db.query(UserFolder).filter(
            UserFolder.user_id == user_id, UserFolder.name == name
        )
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
    ts = _artifact_ts(art)
    return ts is not None and ts + _CLOCK_SLACK_S >= entry.mtime


def _artifact_is_newer(art: Any, entry: MirrorEntry) -> bool:
    """「我的空间」里的版本是否比镜像新 —— 新才需要拉下来覆盖镜像。"""
    ts = _artifact_ts(art)
    return ts is not None and ts > entry.mtime + _CLOCK_SLACK_S


def collect_mirror_changes(
    *,
    user_id: str,
    since_ts: Optional[float] = None,
    baseline: Optional[dict[str, tuple[float, int]]] = None,
    max_bytes: Optional[int] = None,
) -> MirrorChanges:
    """扫描镜像目录，把待登记的改动分成「新文件」和「改了用户已有文件」两类。

    **只判断，不写任何东西**，因此可以安全地放进 HTTP 读路径。bash 传执行前拍的快照
    ``baseline``（拿目录自己和自己比，最可靠）；两个都不传表示全量对账。
    """
    out = MirrorChanges()
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
    chain_memo: dict[tuple[str, ...], _Chain] = {}
    try:
        for entry in iter_mirror_files(user_id, since_ts=since_ts, baseline=baseline):
            out.scanned += 1
            if entry.size > max_bytes:
                out.skipped_too_large += 1
                continue
            folder_names, filename = _ms.split_rel(entry.rel)
            if not filename:
                continue
            # 同一目录下往往有成百上千个文件，目录链按目录缓存，别逐个文件重查一遍
            memo_key = tuple(folder_names)
            chain = chain_memo.get(memo_key)
            if chain is None:
                chain = _resolve_chain(db, user_id, folder_names)
                chain_memo[memo_key] = chain
            art = (
                _artifact_in(db, user_id, chain.folder_id, filename)
                if chain.exists
                else None
            )
            deleted_ts = _ts(getattr(art, "deleted_at", None)) if art is not None else None
            # 文件自己被删、或它所在的文件夹被删，镜像里的都只是残留副本 —— 登记它等于
            # 把用户的删除撤销（真实发生过：整个文件夹被删，里面 2000 多个文件还在镜像里）。
            # 反过来，删除之后沙箱又写了同名文件，那是新内容，按新文件处理。
            residue_ts = max(
                (t for t in (deleted_ts, chain.deleted_ts) if t is not None), default=None
            )
            if residue_ts is not None and residue_ts + _CLOCK_SLACK_S >= entry.mtime:
                out.skipped_deleted += 1
                out.stale.append(entry)
                continue
            if _artifact_is_current(art, entry):
                out.skipped_current += 1
                continue
            if art is None or deleted_ts is not None:
                out.new.append(entry)
            else:
                out.modified.append(entry)
    finally:
        db.close()
    return out


def classify_rels(*, user_id: str, rels: list[str]) -> dict[str, str]:
    """给一批相对路径判定它们在「我的空间」里的状态。

    供沙箱与镜像不是同一份目录的 provider（script_runner / cube）使用 —— 那里拿不到镜像
    mtime，只能按路径查登记记录。返回 ``rel -> "new" | "modified" | "deleted"``。
    """
    out: dict[str, str] = {}
    if not user_id or not rels:
        return out
    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过判定: %s", exc)
        return out

    db = SessionLocal()
    try:
        for rel in rels:
            folder_names, filename = _ms.split_rel(rel)
            if not filename:
                continue
            chain = _resolve_chain(db, user_id, folder_names)
            if chain.deleted_ts is not None:
                out[rel] = "deleted"  # 所在文件夹被用户删了，别重建
                continue
            art = _artifact_in(db, user_id, chain.folder_id, filename) if chain.exists else None
            if art is None:
                out[rel] = "new"
            elif getattr(art, "deleted_at", None) is not None:
                out[rel] = "deleted"
            else:
                out[rel] = "modified"
    finally:
        db.close()
    return out


def register_bytes(
    *,
    user_id: str,
    chat_id: Optional[str],
    rel: str,
    content: bytes,
) -> Optional[dict]:
    """按相对路径把一份字节登记进「我的空间」（已存在则同 file_id 就地更新）。"""
    return _ms.sync_upsert(
        user_id=user_id,
        chat_id=chat_id,
        logical_path=f"{_ms.MYSPACE_LOGICAL}/{rel}",
        content=content,
    )


def register_entry(
    *,
    user_id: str,
    chat_id: Optional[str],
    entry: MirrorEntry,
) -> Optional[dict]:
    """把镜像里的一个文件登记进「我的空间」（已存在则同 file_id 就地更新）。"""
    try:
        content = entry.path.read_bytes()
    except OSError as exc:
        logger.warning("[myspace-mirror] 读取失败 %s: %s", entry.rel, exc)
        return None
    ref = register_bytes(user_id=user_id, chat_id=chat_id, rel=entry.rel, content=content)
    if ref is None and chat_id is not None:
        # 登记失败别把文件丢了：外键之类的会话侧问题不该让用户的文件消失在界面外。
        # 退一步不挂会话再登记一次 —— 少一个来源标注，总好过用户看不见这个文件。
        logger.warning("[myspace-mirror] 挂会话登记失败，改为不挂会话重试: %s", entry.rel)
        ref = register_bytes(user_id=user_id, chat_id=None, rel=entry.rel, content=content)
    return ref


def register_new_files(
    *,
    user_id: str,
    chat_id: Optional[str] = None,
    since_ts: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """只登记新文件的便捷入口：改动已有文件那一类留给带确认门的调用方。"""
    changes = collect_mirror_changes(user_id=user_id, since_ts=since_ts)
    refs: list[dict] = []
    for entry in changes.new[: limit or len(changes.new)]:
        ref = register_entry(user_id=user_id, chat_id=chat_id, entry=entry)
        if ref:
            refs.append(ref)
    if refs:
        logger.info("[myspace-mirror] 登记新文件 user=%s 共 %d 个", user_id, len(refs))
    return refs


def snapshot_mirror_state(user_id: str) -> dict[str, tuple[float, int]]:
    """命令执行前拍下镜像目录的 ``{相对路径: (mtime, 大小)}``，命令跑完拿它做差集。

    两个用途：认出沙箱里**新写/改写**了什么（不依赖任何时钟），以及认出**删掉**了什么
    （``rm`` 不经过任何工具，只能靠前后清单相比）。没有这一步，用户让智能体"把中间文件
    清掉"，界面上纹丝不动，下一条命令的正向同步还会把它们原样拉回来。
    """
    return {e.rel: (e.mtime, e.size) for e in iter_mirror_files(user_id)}


def deleted_since(*, user_id: str, snapshot: dict[str, tuple[float, int]]) -> list[str]:
    """快照里有、现在没了、且在「我的空间」里仍在册的文件 —— 这些删除要同步回去。

    没有登记记录的文件消失了就消失了，本来就不在用户空间里，不必也无从同步。
    """
    if not snapshot:
        return []
    missing = set(snapshot) - {e.rel for e in iter_mirror_files(user_id)}
    if not missing:
        return []
    try:
        from core.db.engine import SessionLocal
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB 不可用，跳过删除同步: %s", exc)
        return []

    db = SessionLocal()
    out: list[str] = []
    try:
        for rel in sorted(missing):
            art = _artifact_for_rel(db, user_id, rel)
            if art is not None and getattr(art, "deleted_at", None) is None:
                out.append(rel)
    finally:
        db.close()
    return out


def delete_registered(*, user_id: str, rel: str) -> bool:
    """把「我的空间」里对应的文件也删掉（软删，和界面上删除同一条路径）。"""
    res = _ms.sync_delete(user_id, f"{_ms.MYSPACE_LOGICAL}/{rel}")
    if res.get("error"):
        logger.warning("[myspace-mirror] 同步删除失败 %s: %s", rel, res["error"])
        return False
    logger.info("[myspace-mirror] 同步删除 user=%s %s", user_id, rel)
    return True


def prune_stale(*, user_id: str, entries: list[MirrorEntry]) -> int:
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


def reconcile_on_read(*, user_id: str) -> list[dict]:
    """读时对账：拉取界面侧的最新状态 + 登记沙箱新写出的文件。

    挂在「我的空间」列表接口上，用户什么时候打开看到的都是当下状态，不必等下一条 bash。
    只登记新文件：改动用户已有文件那一类要过写入确认门，HTTP 读路径没有确认通道。
    按水位增量，第一次全量、之后只看新落盘的文件。
    """
    pull_myspace_updates(user_id=user_id)
    started = time.time()
    since = _read_cursor.get(user_id)
    changes = collect_mirror_changes(user_id=user_id, since_ts=since)  # 内部已让出时钟余量
    batch = changes.new[:_READ_REGISTER_BATCH]
    refs: list[dict] = []
    for entry in batch:
        ref = register_entry(user_id=user_id, chat_id=None, entry=entry)
        if ref:
            refs.append(ref)
    if len(changes.new) > len(batch):
        # 这一轮没登记完（首次遇到大批存量），水位不推进，下次接着做，别把剩下的漏掉。
        return refs
    # 水位取扫描开始时刻并留一点余量：扫描期间落盘的文件下一轮还会被看到，不会漏。
    _read_cursor[user_id] = started - _CLOCK_SLACK_S
    return refs


def _folder_rel(
    db: Any, user_id: str, folder_id: Any, memo: dict[Any, Optional[str]]
) -> Optional[str]:
    """把 folder_id 还原成相对用户根目录的路径；链断了返回 None。

    **包含已删除的目录**：用户删掉整个文件夹后，镜像里的副本还在那条路径下，要把路径算
    出来才能清掉残留。这里只做路径换算，不承担鉴权。
    """
    if folder_id is None:
        return ""
    if folder_id in memo:
        return memo[folder_id]
    from core.db.models import UserFolder

    names: list[str] = []
    cur: Any = folder_id
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = (
            db.query(UserFolder)
            .filter(UserFolder.folder_id == cur, UserFolder.user_id == user_id)
            .first()
        )
        if row is None:
            memo[folder_id] = None
            return None
        names.append(row.name)
        cur = row.parent_folder_id
    rel = "/".join(reversed(names))
    memo[folder_id] = rel
    return rel


def pull_myspace_updates(*, user_id: str) -> PullReport:
    """我的空间 → 镜像目录：界面侧的新增/改动/删除立刻反映到沙箱看得见的地方。

    首次调用（本进程内该用户还没有水位）做一次全量比对，只对镜像里缺失或过期的文件下载；
    之后按 ``updated_at`` 增量。

    删除同样要传导：用户在界面上删掉的文件，镜像里的副本必须一起清掉，否则新会话挂上这份
    目录还看得见它、还会当成上文接着用（真实发生过：某账号删了 2064 个文件，沙箱里全在）。
    删除以 **artifact 记录**为线索，绝不碰"没有登记记录"的文件 —— 那些是等着登记的新文件，
    不是被删的。删除之后沙箱又写了同名文件（镜像更新）则保留，那是新内容。
    """
    rep = PullReport()
    if not user_id:
        return rep
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact
        from core.storage import get_storage
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-mirror] DB/存储不可用，跳过正向同步: %s", exc)
        return rep

    cursor = _pull_cursor.get(user_id)
    first_pass = cursor is None
    now_ts = datetime.now(timezone.utc).timestamp()

    db = SessionLocal()
    try:
        q = db.query(Artifact).filter(
            Artifact.user_id == user_id,
            *personal_artifact_predicates(Artifact),
        )
        if cursor is not None:
            since = datetime.fromtimestamp(cursor, tz=timezone.utc)
            # 删除要单独看 deleted_at：软删只写 deleted_at、不碰 updated_at（见
            # ArtifactRepository.soft_delete_owned），只按 updated_at 增量会把"用户刚在
            # 界面上删掉的文件"整批漏掉，镜像里的副本留着，沙箱照样看得见。
            q = q.filter(or_(Artifact.updated_at >= since, Artifact.deleted_at >= since))
        rows = q.all()
        memo: dict[Any, Optional[str]] = {}
        storage = get_storage()
        for art in rows:
            if not art.filename:
                continue
            folder_rel = _folder_rel(db, user_id, art.user_folder_id, memo)
            if folder_rel is None:
                continue
            filename = str(art.filename)
            rel = f"{folder_rel}/{filename}" if folder_rel else filename
            fp = _ms.myspace_cache_file(user_id, rel)
            deleted_ts = _ts(art.deleted_at)
            if deleted_ts is not None:
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                if mtime > deleted_ts + _CLOCK_SLACK_S:
                    continue  # 删除之后沙箱又写过，是新内容
                _ms._remove_cache(user_id, rel)
                rep.removed += 1
                continue
            entry: Optional[MirrorEntry] = None
            try:
                st = fp.stat()
                entry = MirrorEntry(rel=rel, path=fp, size=st.st_size, mtime=st.st_mtime)
            except OSError:
                entry = None
            # 镜像里没有，或者界面这边确实更新（例如刚重新上传），才拉下来。镜像更新的
            # 情况是沙箱刚写过，留给反向对账登记，别用旧内容盖回去。
            if entry is not None and not _artifact_is_newer(art, entry):
                continue
            try:
                data = storage.download_bytes(str(art.storage_key))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[myspace-mirror] 下载失败 %s: %s", rel, exc)
                rep.failed += 1
                continue
            _ms.mirror_to_cache(user_id, rel, data)
            rep.materialized += 1
    finally:
        db.close()

    _pull_cursor[user_id] = now_ts
    if rep.materialized or rep.removed or rep.failed:
        logger.info(
            "[myspace-mirror] pull user=%s 物化=%d 移除=%d 失败=%d (首轮=%s)",
            user_id, rep.materialized, rep.removed, rep.failed, first_pass,
        )
    return rep


def reset_pull_cursor(user_id: Optional[str] = None) -> None:
    """清空对账水位（测试与手工对账后强制重新全量比对）。"""
    if user_id is None:
        _pull_cursor.clear()
        _read_cursor.clear()
    else:
        _pull_cursor.pop(user_id, None)
        _read_cursor.pop(user_id, None)

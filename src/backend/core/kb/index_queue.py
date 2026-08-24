"""知识库文档索引的持久化作业队列。

索引（解析 → 分块 → 向量化）是分钟级的重活。早先它直接挂在 FastAPI
``BackgroundTasks`` 上，有两个致命问题：

1. **抢请求线程池**。同步后台任务跑在 Starlette/anyio 的共享线程池里（默认 40 个
   令牌），而后端**每个**请求都要经同步依赖 ``get_db`` 拿一个令牌。一次多选几十个
   文件，索引任务就把池占满，此后所有接口在进 handler 之前就排队——前端表现为上传
   一直转圈，网关侧超时直接报 502。同理 DB 连接池（20+10）也会被这批任务顶穿。
2. **进程内、重启即丢**。后端一重建，在飞的索引任务全没了，文档永远停在
   ``processing``，刷新也不会自愈（实测线上留下过这样的僵死文档）。

改法与 Wiki 作业一致：状态落库、常驻 worker 认领、写心跳。这里不另开作业表——
``kb_documents.indexing_status`` 本身就是那张表：

* ``processing``：排队中，等 worker 认领（也是新上传文档的初始态）
* ``indexing``：已认领、正在跑（认领者与心跳写在 ``metadata`` 里）
* ``completed`` / ``failed``：终态

认领是一条 ``UPDATE ... WHERE indexing_status 未变`` 的乐观并发：Postgres 在
READ COMMITTED 下会对并发改动过的行重算 WHERE（EvalPlanQual），SQLite 的写入本就
全局串行，两边都只会有一个赢家，不需要额外的分布式锁。

心跳由 worker 的轮询循环统一续，不需要索引函数内部埋点——超过 ``STALE_AFTER``
没续上的，视为持有者已死，回收成 ``processing`` 重排。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.db.models import KBDocument
from core.infra.logging import get_logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = get_logger(__name__)

# 状态取值
STATUS_QUEUED = "processing"
STATUS_RUNNING = "indexing"
STATUS_DONE = "completed"
STATUS_FAILED = "failed"

# 心跳停摆多久算僵尸。索引单篇可以跑很久（逐块 LLM 抽词/造问都是网络等待），
# 这个窗口必须显著大于最慢的单篇，否则会把还在干活的任务判死、重复索引。
STALE_AFTER = timedelta(minutes=30)

# metadata 里的认领字段
_F_CLAIMED_BY = "indexing_claimed_by"
_F_HEARTBEAT = "indexing_heartbeat"
_F_ATTEMPT = "indexing_attempt"
_F_PARAMS = "indexing_params"

# 单篇最多重试几次。超过就落 failed，避免"必然失败的文档"被反复回收、
# 永远占着 worker 的并发位。
MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(doc: KBDocument) -> Dict[str, Any]:
    return dict(doc.extra_data or {})


def _write_meta(db: Session, doc: KBDocument, meta: Dict[str, Any]) -> None:
    doc.extra_data = meta
    flag_modified(doc, "extra_data")


# ── 入队 ────────────────────────────────────────────────────────────────────


def enqueue(
    db: Session,
    doc: KBDocument,
    *,
    user_id: str,
    chunk_method: str,
    indexing_config: Optional[dict] = None,
    index_modes: Optional[Sequence[str]] = None,
    commit: bool = True,
) -> None:
    """把文档排进索引队列。

    索引参数随文档落库，而不是像以前那样捆在进程内的闭包里——worker 可能是**下一个**
    进程里的（重启续跑），拿不到请求上下文。``user_id`` 也要存：向量行按上传者打标，
    而被授权的协作者上传时它并不等于知识库所有者。
    """
    meta = _meta(doc)
    meta[_F_PARAMS] = {
        "user_id": user_id,
        "chunk_method": chunk_method,
        "indexing_config": indexing_config,
        "index_modes": list(index_modes) if index_modes is not None else None,
    }
    meta[_F_ATTEMPT] = 0
    # 重排时清掉上一轮的认领痕迹与错误，避免刚入队就被当成僵尸回收
    meta.pop(_F_CLAIMED_BY, None)
    meta.pop(_F_HEARTBEAT, None)
    meta.pop("indexing_error", None)
    meta.pop("indexing_failed_at", None)
    _write_meta(db, doc, meta)
    doc.indexing_status = STATUS_QUEUED
    if commit:
        db.commit()


# ── 认领 ────────────────────────────────────────────────────────────────────


def release_stale(db: Session) -> int:
    """把心跳停摆的 ``indexing`` 文档回收成 ``processing``，等下一轮重新认领。

    进程被 kill / 容器重建后，那批文档就靠这里回到队列——这正是老实现缺的一环。
    """
    stale_before = _now() - STALE_AFTER
    candidates = (
        db.query(KBDocument)
        .filter(KBDocument.indexing_status == STATUS_RUNNING, KBDocument.deleted_at.is_(None))
        .limit(200)
        .all()
    )
    released = 0
    for doc in candidates:
        meta = _meta(doc)
        beat = _parse_ts(meta.get(_F_HEARTBEAT))
        if beat is not None and beat > stale_before:
            continue  # 还在跳，别动
        # 抢回收权：同一批多实例只有一个能把状态从 indexing 改回 processing
        won = (
            db.query(KBDocument)
            .filter(
                KBDocument.document_id == doc.document_id,
                KBDocument.indexing_status == STATUS_RUNNING,
            )
            .update({"indexing_status": STATUS_QUEUED}, synchronize_session=False)
        )
        db.commit()
        if not won:
            continue
        logger.warning(
            "[kb-index] 回收僵尸索引任务 %s（原持有者 %s）",
            doc.document_id,
            meta.get(_F_CLAIMED_BY) or "?",
        )
        released += 1
    return released


def claim_next(db: Session, worker_id: str) -> Optional[Dict[str, Any]]:
    """认领一篇排队中的文档，返回执行所需的快照（纯 dict，可跨线程用）。

    返回 dict 而不是 ORM 对象：真正的索引跑在别的线程、用自己的 session，
    把绑定在本 session 上的实例带过去必然踩到跨线程复用连接的坑。
    """
    candidate = (
        db.query(KBDocument)
        .filter(KBDocument.indexing_status == STATUS_QUEUED, KBDocument.deleted_at.is_(None))
        .order_by(KBDocument.uploaded_at)
        .first()
    )
    if candidate is None:
        return None

    document_id = candidate.document_id
    won = (
        db.query(KBDocument)
        .filter(
            KBDocument.document_id == document_id,
            KBDocument.indexing_status == STATUS_QUEUED,
        )
        .update({"indexing_status": STATUS_RUNNING}, synchronize_session=False)
    )
    db.commit()
    if not won:
        return None  # 被别的实例抢走了，下一轮再来

    doc = db.query(KBDocument).filter(KBDocument.document_id == document_id).first()
    if doc is None:  # 认领与删除撞上了
        return None

    meta = _meta(doc)
    attempt = int(meta.get(_F_ATTEMPT) or 0) + 1
    if attempt > MAX_ATTEMPTS:
        # 已经试到头了就别再放它进来。这条守卫针对的是"跑着跑着把进程带走"的文档
        # （比如解析时 OOM）：那种任务不会走到失败分支，只会被僵尸回收重排，没有
        # 上限就会一直循环，永远占着一个并发位。
        meta["indexing_error"] = f"索引连续失败 {MAX_ATTEMPTS} 次，已停止重试"
        meta["indexing_failed_at"] = datetime.utcnow().isoformat()
        meta.pop(_F_CLAIMED_BY, None)
        meta.pop(_F_HEARTBEAT, None)
        _write_meta(db, doc, meta)
        doc.indexing_status = STATUS_FAILED
        db.commit()
        logger.error("[kb-index] 文档 %s 重试已达上限，置 failed", document_id)
        return None
    meta[_F_ATTEMPT] = attempt
    meta[_F_CLAIMED_BY] = worker_id
    meta[_F_HEARTBEAT] = _now().isoformat()
    _write_meta(db, doc, meta)
    db.commit()

    params = meta.get(_F_PARAMS) or {}
    fallback = _params_from_space(db, doc.kb_id) if not params else {}
    return {
        "document_id": doc.document_id,
        "kb_id": doc.kb_id,
        "title": doc.title,
        "mime_type": doc.mime_type,
        "storage_key": doc.storage_key,
        "attempt": attempt,
        "user_id": params.get("user_id") or fallback.get("user_id") or "",
        "chunk_method": params.get("chunk_method")
        or fallback.get("chunk_method")
        or "structured",
        "indexing_config": params.get("indexing_config") or fallback.get("indexing_config"),
        "index_modes": params.get("index_modes"),
    }


def _params_from_space(db: Session, kb_id: str) -> Dict[str, Any]:
    """老文档没有随身参数时，从知识库现配置回推一套。

    升级到本队列之前入库、且还停在 ``processing`` 的文档没有 ``indexing_params``
    （它们的参数原本活在已经消失的那个进程里）。这些文档 worker 一启动就会接手，
    所以必须给出**合理**的默认——而不是硬编码一个可能与该库分块方式不符的值，
    也不能把 ``user_id`` 留空（向量行按它打标）。
    """
    from core.db.models import KBSpace

    space = db.query(KBSpace).filter(KBSpace.kb_id == kb_id).first()
    if space is None:
        return {}
    extra = space.extra_data if isinstance(space.extra_data, dict) else {}
    return {
        "user_id": space.user_id,
        "chunk_method": space.chunk_method,
        "indexing_config": extra.get("indexing_config"),
    }


def heartbeat(db: Session, document_ids: Sequence[str]) -> None:
    """给在飞的文档统一续心跳。

    索引函数本身是一整段阻塞调用，没有可插桩的进度点——所以心跳由 worker 的轮询
    循环代劳：只要这个进程还活着，它就在续。
    """
    if not document_ids:
        return
    stamp = _now().isoformat()
    docs = db.query(KBDocument).filter(KBDocument.document_id.in_(list(document_ids))).all()
    for doc in docs:
        meta = _meta(doc)
        meta[_F_HEARTBEAT] = stamp
        _write_meta(db, doc, meta)
    db.commit()


def give_up_or_requeue(db: Session, document_id: str, error: str) -> None:
    """任务异常收场：还有重试额度就退回队列，否则落 ``failed``。"""
    doc = db.query(KBDocument).filter(KBDocument.document_id == document_id).first()
    if doc is None:
        return
    meta = _meta(doc)
    attempt = int(meta.get(_F_ATTEMPT) or 0)
    if attempt < MAX_ATTEMPTS:
        meta.pop(_F_CLAIMED_BY, None)
        meta.pop(_F_HEARTBEAT, None)
        _write_meta(db, doc, meta)
        doc.indexing_status = STATUS_QUEUED
        db.commit()
        logger.warning(
            "[kb-index] 文档 %s 第 %d 次索引失败，退回队列重试：%s", document_id, attempt, error
        )
        return
    meta["indexing_error"] = error[:1000]
    meta["indexing_failed_at"] = datetime.utcnow().isoformat()
    meta.pop(_F_CLAIMED_BY, None)
    meta.pop(_F_HEARTBEAT, None)
    _write_meta(db, doc, meta)
    doc.indexing_status = STATUS_FAILED
    db.commit()
    logger.error("[kb-index] 文档 %s 重试 %d 次仍失败，置 failed：%s", document_id, attempt, error)


def queue_depth(db: Session) -> int:
    return (
        db.query(KBDocument)
        .filter(KBDocument.indexing_status == STATUS_QUEUED, KBDocument.deleted_at.is_(None))
        .count()
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def pending_document_ids(db: Session, kb_id: str) -> List[str]:
    """某知识库里还没索引完的文档 ID（排队中 + 正在跑）。"""
    rows = (
        db.query(KBDocument.document_id)
        .filter(
            KBDocument.kb_id == kb_id,
            KBDocument.deleted_at.is_(None),
            KBDocument.indexing_status.in_((STATUS_QUEUED, STATUS_RUNNING)),
        )
        .all()
    )
    return [r[0] for r in rows]

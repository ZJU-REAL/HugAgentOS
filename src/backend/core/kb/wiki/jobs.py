"""Wiki 作业的入队与认领。

Wiki 生成是分钟级到小时级的长任务，不能挂在请求生命周期上（BackgroundTasks
在容器重启时会把跑到一半的作业整个丢掉，用户看到的是半截目录）。所以作业状态
一律落 ``kb_wiki_jobs``，worker 认领后写心跳，重启时按心跳超时回收僵尸重跑。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from core.db.models import KBWikiJob
from core.kb.wiki.store import _new_id
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

JOB_INGEST = "ingest"
JOB_RETRACT = "retract"
JOB_FINALIZE = "finalize"

# 心跳超过这个时长没更新，就认为持有者已经死了，作业可被重新认领
STALE_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    return _new_id("kbwj")


def _enqueue_documents(
    db: Session, kb_id: str, document_ids: Sequence[str], job_type: str
) -> Optional[KBWikiJob]:
    ids = [str(d).strip() for d in document_ids if str(d or "").strip()]
    if not kb_id or not ids:
        return None
    job = KBWikiJob(
        job_id=_new_job_id(),
        kb_id=kb_id,
        job_type=job_type,
        payload={"document_ids": ids},
        status="pending",
        progress={"stage": "排队中", "done": 0, "total": len(ids)},
    )
    db.add(job)
    db.commit()
    logger.info("Wiki %s 作业入队 kb=%s docs=%d job=%s", job_type, kb_id, len(ids), job.job_id)
    return job


def enqueue_ingest(db: Session, kb_id: str, document_ids: Sequence[str]) -> Optional[KBWikiJob]:
    """为一批文档排一个摄入作业。

    同一个知识库允许多个 pending ingest 并存——它们认领的是互不相交的文档，
    并发跑没有问题，也让大批量上传不至于排成一条长队。
    """
    return _enqueue_documents(db, kb_id, document_ids, JOB_INGEST)


def enqueue_retract(db: Session, kb_id: str, document_ids: Sequence[str]) -> Optional[KBWikiJob]:
    """文档被删后排一个血缘摘除作业。"""
    return _enqueue_documents(db, kb_id, document_ids, JOB_RETRACT)


def _find_open_finalize(db: Session, kb_id: str) -> Optional[KBWikiJob]:
    return (
        db.query(KBWikiJob)
        .filter(
            KBWikiJob.kb_id == kb_id,
            KBWikiJob.job_type == JOB_FINALIZE,
            KBWikiJob.status.in_(("pending", "running")),
        )
        .first()
    )


def enqueue_finalize(db: Session, kb_id: str) -> Optional[KBWikiJob]:
    """排一个收尾作业；同一知识库已有未完成的收尾时直接复用，不重复排。

    库上有部分唯一索引兜底，这里的先查后插只是省掉一次必然失败的插入；两个进程
    同时排队时靠 IntegrityError 收敛。
    """
    if not kb_id:
        return None
    existing = _find_open_finalize(db, kb_id)
    if existing:
        return existing

    job = KBWikiJob(
        job_id=_new_job_id(),
        kb_id=kb_id,
        job_type=JOB_FINALIZE,
        payload={},
        status="pending",
        progress={"stage": "排队中", "done": 0, "total": 1},
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # 另一个进程刚好排了同一个收尾——那正是我们想要的结果
        db.rollback()
        return _find_open_finalize(db, kb_id)
    return job


def claim_next_job(db: Session, worker_id: str) -> Optional[KBWikiJob]:
    """认领一个待跑作业。

    认领包含两类：从未跑过的 pending，以及心跳早已停摆的僵尸 running。用
    ``UPDATE ... WHERE status 未变`` 的乐观并发收敛，多实例同时抢也只有一个能改到。
    """
    stale_before = _now() - STALE_AFTER
    candidate = (
        db.query(KBWikiJob)
        .filter(
            or_(
                KBWikiJob.status == "pending",
                (KBWikiJob.status == "running") & (KBWikiJob.heartbeat_at < stale_before),
            )
        )
        # 收尾排在摄入之后：一批文档还没写完就收尾，索引页会立刻过期
        .order_by(KBWikiJob.job_type.desc(), KBWikiJob.created_at)
        .first()
    )
    if candidate is None:
        return None

    previous_status = candidate.status
    updated = (
        db.query(KBWikiJob)
        .filter(KBWikiJob.job_id == candidate.job_id, KBWikiJob.status == previous_status)
        .update(
            {
                "status": "running",
                "claimed_by": worker_id,
                "claimed_at": _now(),
                "heartbeat_at": _now(),
                "attempt": KBWikiJob.attempt + 1,
                "updated_at": _now(),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        return None  # 被别人抢走了，下一轮再来
    db.refresh(candidate)
    if previous_status == "running":
        logger.warning(
            "回收僵尸 Wiki 作业 %s（原持有者 %s）", candidate.job_id, candidate.claimed_by
        )
    return candidate


def claim_sibling_ingest_jobs(
    db: Session, worker_id: str, kb_id: str, limit: int
) -> List[KBWikiJob]:
    """再认领同一知识库的若干个待跑摄入作业，与首个作业合并成一批处理。

    上传是一篇一个作业（入队点在文档索引完成的回调里，天然是单篇），但**处理**
    没有理由也一篇一批：合批之后 Map 阶段的 ``map_parallel`` 才真的有多篇文档可
    并行，全库页面表也只加载一次而不是每篇一次。39 篇串行跑要数小时，合批后是
    数十分钟量级。

    只合并同库的 pending 作业——跨库合并会让一个大库饿死其他库。
    """
    if limit <= 0:
        return []
    candidates = (
        db.query(KBWikiJob)
        .filter(
            KBWikiJob.kb_id == kb_id,
            KBWikiJob.job_type == JOB_INGEST,
            KBWikiJob.status == "pending",
        )
        .order_by(KBWikiJob.created_at)
        .limit(limit)
        .all()
    )
    claimed: List[KBWikiJob] = []
    for job in candidates:
        updated = (
            db.query(KBWikiJob)
            .filter(KBWikiJob.job_id == job.job_id, KBWikiJob.status == "pending")
            .update(
                {
                    "status": "running",
                    "claimed_by": worker_id,
                    "claimed_at": _now(),
                    "heartbeat_at": _now(),
                    "attempt": KBWikiJob.attempt + 1,
                    "updated_at": _now(),
                },
                synchronize_session=False,
            )
        )
        if updated:
            claimed.append(job)
    db.commit()
    if claimed:
        logger.info("Wiki 合批：额外认领 %d 个同库摄入作业 kb=%s", len(claimed), kb_id)
    return claimed


def finish_jobs(db: Session, job_ids: Sequence[str], *, progress: Optional[dict] = None) -> None:
    """批量结单。合批处理时首个作业与被并入的兄弟作业一起收尾。"""
    if not job_ids:
        return
    db.query(KBWikiJob).filter(KBWikiJob.job_id.in_(list(job_ids))).update(
        {
            "status": "succeeded",
            "finished_at": _now(),
            "updated_at": _now(),
            "last_error": None,
            **({"progress": progress} if progress is not None else {}),
        },
        synchronize_session=False,
    )
    db.commit()


def heartbeat(db: Session, job_id: str, progress: Optional[dict] = None) -> None:
    values = {"heartbeat_at": _now(), "updated_at": _now()}
    if progress is not None:
        values["progress"] = progress
    db.query(KBWikiJob).filter(KBWikiJob.job_id == job_id).update(values, synchronize_session=False)
    db.commit()


def finish_job(db: Session, job_id: str, *, progress: Optional[dict] = None) -> None:
    db.query(KBWikiJob).filter(KBWikiJob.job_id == job_id).update(
        {
            "status": "succeeded",
            "finished_at": _now(),
            "updated_at": _now(),
            "last_error": None,
            **({"progress": progress} if progress is not None else {}),
        },
        synchronize_session=False,
    )
    db.commit()


def fail_job(db: Session, job: KBWikiJob, error: str) -> None:
    """作业失败：还有重试机会就退回 pending，否则终结。"""
    exhausted = int(job.attempt or 0) >= int(job.max_attempts or 3)
    db.query(KBWikiJob).filter(KBWikiJob.job_id == job.job_id).update(
        {
            "status": "failed" if exhausted else "pending",
            "last_error": str(error)[:2000],
            "updated_at": _now(),
            **({"finished_at": _now()} if exhausted else {}),
        },
        synchronize_session=False,
    )
    db.commit()
    if exhausted:
        logger.error("Wiki 作业 %s 重试耗尽，终止：%s", job.job_id, error)
    else:
        logger.warning("Wiki 作业 %s 失败，将重试（第 %s 次）：%s", job.job_id, job.attempt, error)


def kb_job_summary(db: Session, kb_id: str) -> dict:
    """某知识库的 Wiki 作业概览，供前端展示生成进度。"""
    rows = (
        db.query(KBWikiJob)
        .filter(KBWikiJob.kb_id == kb_id)
        .order_by(KBWikiJob.created_at.desc())
        .limit(20)
        .all()
    )
    running = [r for r in rows if r.status == "running"]
    pending = [r for r in rows if r.status == "pending"]
    failed = [r for r in rows if r.status == "failed"]
    active = running[0] if running else None
    return {
        "running": len(running),
        "pending": len(pending),
        "failed": len(failed),
        "generating": bool(running or pending),
        "progress": (active.progress if active else {}) or {},
        "last_error": failed[0].last_error if failed else None,
    }


def pending_document_ids(db: Session, kb_id: str) -> List[str]:
    """还排在队列里等待处理的文档 ID（去重）。"""
    rows = (
        db.query(KBWikiJob)
        .filter(
            KBWikiJob.kb_id == kb_id,
            KBWikiJob.job_type == JOB_INGEST,
            KBWikiJob.status.in_(("pending", "running")),
        )
        .all()
    )
    collected: List[str] = []
    for row in rows:
        for document_id in (row.payload or {}).get("document_ids", []):
            if document_id not in collected:
                collected.append(document_id)
    return collected

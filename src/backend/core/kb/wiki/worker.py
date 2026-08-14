"""Wiki 生成的常驻 worker。

形态与自动化调度器一致：应用启动时拉起一个 asyncio 任务轮询作业表，真正的
生成跑在线程里（整条链路是阻塞式的），跑完排下一个。

多实例部署时靠 ``kb_wiki_jobs`` 上的乐观并发认领收敛，不需要额外的分布式锁——
认领是一条 ``UPDATE ... WHERE status 未变``，只有一个实例能改到。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from typing import Optional

from core.infra.logging import get_logger
from core.kb.wiki import jobs as job_queue
from core.kb.wiki.config import WikiConfig, resolve_wiki_config, wiki_enabled
from core.kb.wiki.finalize import finalize_kb_wiki
from core.kb.wiki.llm import LLMBudget, WikiQuotaExceeded, wiki_model_configured
from core.kb.wiki.pipeline import run_ingest, run_retract

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = float(os.getenv("KB_WIKI_POLL_INTERVAL_SECONDS", "10"))
_IDLE_BACKOFF_SECONDS = float(os.getenv("KB_WIKI_IDLE_BACKOFF_SECONDS", "30"))


class WikiIngestWorker:
    """轮询 kb_wiki_jobs 并执行的后台 worker。"""

    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="kb-wiki-worker")
        logger.info("[wiki-worker] 已启动 worker_id=%s", self.worker_id)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("[wiki-worker] 已停止")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                worked = await asyncio.to_thread(self._run_one)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 轮询循环绝不能因单次异常退出
                logger.error("[wiki-worker] 轮询异常", exc_info=True)
                worked = False
            delay = _POLL_INTERVAL_SECONDS if worked else _IDLE_BACKOFF_SECONDS
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    # ── 单个作业 ────────────────────────────────────────────────────────────

    def _run_one(self) -> bool:
        """认领并执行一个作业。返回是否真的干了活（决定下一轮的等待时长）。"""
        from core.db.engine import SessionLocal
        from core.db.models import KBSpace

        with SessionLocal() as db:
            job = job_queue.claim_next_job(db, self.worker_id)
            if job is None:
                return False

            space = (
                db.query(KBSpace)
                .filter(KBSpace.kb_id == job.kb_id, KBSpace.deleted_at.is_(None))
                .one_or_none()
            )
            if space is None:
                logger.info("[wiki-worker] 知识库 %s 已删除，跳过作业 %s", job.kb_id, job.job_id)
                job_queue.finish_job(db, job.job_id, progress={"stage": "知识库已删除"})
                return True
            if not wiki_enabled(space):
                logger.info(
                    "[wiki-worker] 知识库 %s 未开启 Wiki，跳过作业 %s", job.kb_id, job.job_id
                )
                job_queue.finish_job(db, job.job_id, progress={"stage": "未开启 Wiki"})
                return True
            if not wiki_model_configured():
                job_queue.fail_job(
                    db,
                    job,
                    "未配置 Wiki 生成模型：请在模型管理中为「知识库 Wiki 实体抽取」角色绑定供应商",
                )
                return True

            config = resolve_wiki_config(space)
            try:
                self._dispatch(db, job, config)
            except WikiQuotaExceeded as exc:
                # 超额不是错误，是设计好的刹车：已生成的部分保留，作业正常结束
                logger.warning("[wiki-worker] 作业 %s 触及调用上限: %s", job.job_id, exc)
                job_queue.finish_job(db, job.job_id, progress={"stage": "已达本次生成上限"})
            except Exception as exc:  # noqa: BLE001 - 失败要落库并按重试策略处理
                logger.error("[wiki-worker] 作业 %s 执行失败", job.job_id, exc_info=True)
                db.rollback()
                job_queue.fail_job(db, job, str(exc))
            return True

    def _dispatch(self, db, job, config: WikiConfig) -> None:
        document_ids = list((job.payload or {}).get("document_ids") or [])

        if job.job_type == job_queue.JOB_RETRACT:
            stats = run_retract(db, job.kb_id, document_ids)
            job_queue.finish_job(db, job.job_id, progress={"stage": "已完成", **stats})
            job_queue.enqueue_finalize(db, job.kb_id)
            return

        if job.job_type == job_queue.JOB_FINALIZE:
            budget = LLMBudget(max_calls=max(2, config.max_llm_calls // 10))
            stats = finalize_kb_wiki(db, job.kb_id, config=config, budget=budget)
            db.commit()
            job_queue.finish_job(db, job.job_id, progress={"stage": "已完成", **stats})
            return

        # ingest：把同库的其他待跑作业一并认领，合成一批处理
        siblings = job_queue.claim_sibling_ingest_jobs(
            db, self.worker_id, job.kb_id, config.batch_size - 1
        )
        job_ids = [job.job_id, *(s.job_id for s in siblings)]
        for sibling in siblings:
            for doc_id in (sibling.payload or {}).get("document_ids", []):
                if doc_id not in document_ids:
                    document_ids.append(doc_id)

        budget = LLMBudget(max_calls=config.max_llm_calls)

        def _progress(stage: str, done: int, total: int) -> None:
            for job_id in job_ids:
                job_queue.heartbeat(
                    db, job_id, progress={"stage": stage, "done": done, "total": total}
                )

        result = run_ingest(
            db,
            job.kb_id,
            document_ids,
            config=config,
            budget=budget,
            progress=_progress,
        )
        job_queue.finish_jobs(
            db,
            job_ids,
            progress={
                "stage": "已完成",
                "done": result.documents,
                "total": result.documents,
                "pages_written": result.pages_written,
                "summaries_written": result.summaries_written,
                **budget.snapshot(),
            },
        )
        job_queue.enqueue_finalize(db, job.kb_id)

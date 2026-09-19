"""Queue worker for persona distillation jobs.

作业本来就是一行有生命周期的记录（``queued → running → completed/failed``），派发却曾经
是创建它的那个请求里的一句 ``asyncio.create_task``——既要求请求跑在事件循环上（路由改成
同步 ``def`` 后就不成立了），又让作业的存活挂在那个协程上。这里把派发交还给记录本身：
路由只写 ``queued``，认领与执行由工人负责。

复用 ``_worker_base`` 的三件套（``claim_next`` / ``recover_stale`` / ``drain_queue``），
与技能蒸馏 cron 用的是同一套，所以并发认领、孤儿重排的语义只有一份。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from core.db.models import PersonaDistillJob
from core.infra.logging import get_logger
from orchestration.schedulers._worker_base import (
    claim_next,
    drain_queue,
    interruptible_sleep,
    recover_stale,
)

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 5
# 一个作业自己就带 map 阶段的并发，再叠一层只会同时压垮模型配额。
CONCURRENCY = 1
# 超过这个时长仍停在 running 视作孤儿。必须宽于一个作业的正常耗时，否则会把活着的作业
# 从另一个 worker 手里抢走重跑。
STALE_TIMEOUT_MINUTES = 120

_LOG_TAG = "persona-distill"


class PersonaDistillWorker:
    """认领 queued 的人格蒸馏作业并执行。"""

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[%s] worker started", _LOG_TAG)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[%s] worker stopped", _LOG_TAG)

    async def _loop(self) -> None:
        await asyncio.sleep(5)  # 让应用先启动完
        while self._running:
            try:
                await asyncio.to_thread(
                    recover_stale,
                    PersonaDistillJob,
                    started_field="started_at",
                    timeout_minutes=STALE_TIMEOUT_MINUTES,
                    log_tag=_LOG_TAG,
                )
                await drain_queue(
                    claim=_claim_next_job,
                    process=_process_job,
                    concurrency=CONCURRENCY,
                    log_tag=_LOG_TAG,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一次失败不能让工人退出
                logger.error("[%s] drain error: %s", _LOG_TAG, exc, exc_info=True)
            await interruptible_sleep(POLL_INTERVAL_SECONDS, lambda: self._running)


def _claim_next_job() -> Optional[str]:
    """原子地把一条 queued 作业翻成 running 并返回 job_id。"""
    return claim_next(PersonaDistillJob, pk_field="job_id")


async def _process_job(job_id: str) -> None:
    from core.services.persona_distillation_service import run_job

    await run_job(job_id)

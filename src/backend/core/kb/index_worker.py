"""知识库文档索引的常驻 worker。

形态与 Wiki worker 一致：应用启动时拉起一个 asyncio 任务轮询队列，真正的索引跑在
**自己的**线程池里。

"自己的线程池"是这个模块存在的理由。索引以前用 FastAPI ``BackgroundTasks``，同步
任务会落进 Starlette/anyio 的**共享请求线程池**——而后端每个请求都要经同步依赖
``get_db`` 从同一个池里拿令牌。一次多选几十个文件，索引就把池占满，所有接口在进
handler 之前排队，前端一直转圈、网关报 502。这里换成独立的
``ThreadPoolExecutor``，并发上限由 ``KB_INDEX_CONCURRENCY`` 控制：索引再多也只会
排在自己的队里，碰不到请求线程池，也不会把 DB 连接池顶穿。

队列语义与认领/心跳/回收见 :mod:`core.kb.index_queue`。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from core.infra.logging import get_logger
from core.kb import index_queue

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = float(os.getenv("KB_INDEX_POLL_INTERVAL_SECONDS", "3"))
_IDLE_BACKOFF_SECONDS = float(os.getenv("KB_INDEX_IDLE_BACKOFF_SECONDS", "10"))


def _concurrency() -> int:
    """同时索引几篇。

    默认 3：单篇索引里最重的是 embedding 与逐块 LLM 抽词，都是网络等待，开太大只会
    让向量服务和 DB 连接池吃紧（历史上 39 篇并发直接把 QueuePool 打超时）。
    """
    try:
        value = int(os.getenv("KB_INDEX_CONCURRENCY", "3"))
    except ValueError:
        value = 3
    return max(1, min(value, 16))


class KBIndexWorker:
    """轮询 ``kb_documents`` 索引队列并执行的后台 worker。"""

    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.concurrency = _concurrency()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._inflight: Dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="kb-index"
        )
        self._task = asyncio.create_task(self._loop(), name="kb-index-worker")
        logger.info(
            "[kb-index] 已启动 worker_id=%s 并发=%d", self.worker_id, self.concurrency
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pool is not None:
            # 不等在飞的任务跑完：容器停机只有几十秒，等不到分钟级的索引。没跑完的
            # 文档心跳会停摆，下个进程按僵尸回收重排——这正是持久化队列的意义。
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        logger.info("[kb-index] 已停止")

    # ── 轮询 ────────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        first_round = True
        while not self._stopping.is_set():
            worked = False
            try:
                if first_round:
                    # 启动即接手上个进程留下的僵死文档（老实现里它们会永远停在
                    # processing，刷新也不自愈）
                    await asyncio.to_thread(self._release_stale)
                    first_round = False
                await asyncio.to_thread(self._beat)
                worked = await self._fill_slots()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 轮询循环绝不能因单次异常退出
                logger.error("[kb-index] 轮询异常", exc_info=True)
            delay = _POLL_INTERVAL_SECONDS if (worked or self._inflight) else _IDLE_BACKOFF_SECONDS
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def _fill_slots(self) -> bool:
        """把空闲并发位填满，返回这轮是否真的领到了活。"""
        claimed_any = False
        while len(self._inflight) < self.concurrency and not self._stopping.is_set():
            snapshot = await asyncio.to_thread(self._claim_one)
            if snapshot is None:
                break
            claimed_any = True
            self._spawn(snapshot)
        return claimed_any

    def _spawn(self, snapshot: dict) -> None:
        document_id = snapshot["document_id"]
        loop = asyncio.get_running_loop()
        assert self._pool is not None
        # loop 一并交给工作线程：索引链路里的图像理解要把协程投回**这个**循环执行
        # （见 core/kb/kb_assets.py::_describe_batch）。这个池是我们自己的，不是 anyio
        # 的工作线程池，线程里既没有运行中的循环、也没有回到宿主循环的通路。
        future = loop.run_in_executor(self._pool, self._run_one, snapshot, loop)
        self._inflight[document_id] = future

        def _done(fut: asyncio.Future) -> None:
            self._inflight.pop(document_id, None)
            exc = None
            with contextlib.suppress(asyncio.CancelledError):
                exc = fut.exception()
            if exc is not None:
                logger.error("[kb-index] 文档 %s 索引线程异常", document_id, exc_info=exc)

        future.add_done_callback(_done)

    # ── 同步侧（跑在线程里） ────────────────────────────────────────────────

    def _release_stale(self) -> None:
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            released = index_queue.release_stale(db)
        if released:
            logger.info("[kb-index] 启动回收了 %d 篇僵死文档，已重新排队", released)

    def _beat(self) -> None:
        if not self._inflight:
            return
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            index_queue.heartbeat(db, list(self._inflight.keys()))

    def _claim_one(self) -> Optional[dict]:
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            return index_queue.claim_next(db, self.worker_id)

    def _run_one(self, snapshot: dict, loop: "asyncio.AbstractEventLoop | None" = None) -> None:
        """真正干活：从存储取回原文，跑完整条索引链路。

        ``loop`` 是调度这次索引的宿主事件循环，用于把异步子系统（当前是图像理解的
        视觉桥）的协程投回主循环执行。
        """
        from core.content.kb_processing import vectorise_document_background
        from core.storage import get_storage

        document_id = snapshot["document_id"]
        try:
            file_bytes = get_storage().download_bytes(snapshot["storage_key"])
        except Exception as exc:  # noqa: BLE001 - 取不回原文按一次失败计
            from core.db.engine import SessionLocal

            with SessionLocal() as db:
                index_queue.give_up_or_requeue(
                    db, document_id, f"读取原文失败: {type(exc).__name__}: {exc}"
                )
            return

        logger.info(
            "[kb-index] 开始索引 %s（第 %d 次尝试，%d 字节）",
            document_id,
            snapshot.get("attempt", 1),
            len(file_bytes),
        )
        try:
            vectorise_document_background(
                document_id=document_id,
                kb_id=snapshot["kb_id"],
                user_id=snapshot["user_id"],
                title=snapshot["title"],
                file_bytes=file_bytes,
                mime_type=snapshot["mime_type"],
                chunk_method=snapshot["chunk_method"],
                db_url=os.getenv("DATABASE_URL", ""),
                indexing_config=snapshot.get("indexing_config"),
                index_modes=snapshot.get("index_modes"),
                loop=loop,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：索引函数内部已自行落 failed
            from core.db.engine import SessionLocal

            with SessionLocal() as db:
                index_queue.give_up_or_requeue(
                    db, document_id, f"{type(exc).__name__}: {exc}"
                )

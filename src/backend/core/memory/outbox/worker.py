"""Durable memory outbox: dispatch, draining and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from core.db.models import MemoryOutbox
from core.db.threads import run_db, run_in_session
from core.memory.effect_lane import EffectLaneDeferred, ordered_l2_effect

from core.memory.outbox import common
from core.memory.outbox import jobs
from core.memory.outbox import leases
from core.memory.outbox import settlement

logger = logging.getLogger(__name__)


def _claimed_row(db, job_id: str, worker_id: str) -> Optional[MemoryOutbox]:
    row = db.get(MemoryOutbox, job_id)
    if row is None or row.status != "processing" or row.lease_owner != worker_id:
        return None
    db.expunge(row)
    return row


async def _process_claimed(job_id: str, worker_id: str) -> None:
    row = await run_in_session(_claimed_row, job_id, worker_id)
    if row is None:
        return

    owner_task = asyncio.current_task()
    if owner_task is None:  # pragma: no cover - asyncio owns this coroutine
        return
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        leases._lease_heartbeat(job_id, worker_id, lease_lost, owner_task)
    )
    try:
        from core.memory.pipeline import get_background_semaphore

        result: Any
        async with get_background_semaphore():
            if not await run_db(leases._owns_lease, job_id, worker_id):
                lease_lost.set()
                return
            if row.job_kind == "pipeline":
                result = await jobs._process_pipeline(row)
            elif row.job_kind == "candidate":
                if row.layer == "L2:procedural" and row.scope_key:
                    async with ordered_l2_effect(row.scope_key, row.id):
                        result = await jobs._process_candidate(row)
                else:
                    result = await jobs._process_candidate(row)
            elif row.job_kind == "profile_compact":
                result = await jobs._process_profile_compact(row)
            elif row.job_kind == "profile_edit":
                result = await jobs._process_profile_edit(row)
            elif row.job_kind == "memory_edit":
                result = await jobs._process_memory_edit(row)
            elif row.job_kind == "settlement":
                result = await jobs._process_settlement(row)
            else:
                raise RuntimeError(f"unknown memory outbox job kind: {row.job_kind}")
    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("[memory_outbox] lease lost; stopped job id=%s", job_id)
            return
        raise
    except EffectLaneDeferred as exc:
        await run_db(leases._defer_claim, job_id, worker_id, exc.retry_at)
    except Exception as exc:
        logger.warning("[memory_outbox] job failed id=%s: %s", job_id, exc)
        await run_db(settlement._finish_failure, job_id, worker_id, exc)
    else:
        try:
            await run_db(settlement._finish_success, job_id, worker_id, result)
        except Exception as exc:
            # Processing can succeed while the atomic local acknowledgement
            # fails (for example the assistant message has not committed yet).
            # Roll the owned row into retry instead of parking it until lease
            # expiry; external receipts make replay safe.
            logger.warning("[memory_outbox] acknowledgement failed id=%s: %s", job_id, exc)
            await run_db(settlement._finish_failure, job_id, worker_id, exc)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def drain_outbox(*, max_jobs: int = 100, worker_id: Optional[str] = None) -> int:
    worker = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    processed = 0
    while processed < max_jobs:
        job_id = await run_db(leases._claim_next, worker)
        if job_id is None:
            break
        await _process_claimed(job_id, worker)
        processed += 1
    return processed


async def consume_outbox_job(job_id: str, *, wait_s: float = 5.0) -> dict[str, Any]:
    """Try an admitted interactive edit now, while leaving retry durability intact."""

    worker_id = f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if await run_db(leases._claim_specific, job_id, worker_id):
        await _process_claimed(job_id, worker_id)
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_s)
    while True:
        snapshot = await run_db(leases.get_outbox_job, job_id)
        if snapshot is None:
            raise RuntimeError(f"outbox job disappeared: {job_id}")
        if snapshot["status"] in common._TERMINAL or snapshot["status"] == "retry":
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            return snapshot
        await asyncio.sleep(0.05)


def kick_outbox_drain() -> None:
    """Wake the lifecycle-owned worker; never create an orphan consumer task."""

    worker = process_state().active_worker
    if worker is not None:
        worker.wake()


class MemoryOutboxWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    async def start(self) -> None:
        pass
        if self._task is None or self._task.done():
            self._stop.clear()
            self._wake.clear()
            await run_db(settlement.reconcile_settlement_jobs)
            process_state().active_worker = self
            self._task = asyncio.create_task(self._loop(), name="memory-outbox-worker")

    async def stop(self) -> None:
        pass
        self._stop.set()
        self._wake.set()
        if process_state().active_worker is self:
            process_state().active_worker = None
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await run_db(leases._release_worker_leases, self.worker_id)
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await drain_outbox(max_jobs=100, worker_id=self.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[memory_outbox] worker drain failed; retrying")
                processed = 0
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=common.settings.memory.outbox_poll_interval_s
                )
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()


@dataclass
class WorkerState:
    active_worker: Optional['MemoryOutboxWorker'] = None


_STATE = WorkerState()


def process_state() -> WorkerState:
    return _STATE

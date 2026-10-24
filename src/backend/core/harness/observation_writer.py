"""Bounded background persistence for hook-path observability records.

Hook stages and framework events are recorded on every model and tool step.
Waiting for their database writes would put observability on the execution
critical path, so they are queued here and written by one daemon thread.
``submit`` never blocks: a full queue drops the record and logs the loss.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

MAX_PENDING = 10_000
MAX_BATCH = 500
DROP_LOG_EVERY = 1_000

BatchWrite = Callable[[Any, Sequence[Any]], Any]


class ObservationWriter:
    """Drain ``(sink, write, run_id, records)`` items in per-sink, per-run batches."""

    def __init__(self, *, max_pending: int = MAX_PENDING, max_batch: int = MAX_BATCH) -> None:
        self._max_pending = max(1, int(max_pending))
        self._max_batch = max(1, int(max_batch))
        self._lock = threading.Lock()
        self._queue: queue.Queue | None = None
        self._pid: int | None = None
        self.dropped = 0

    def _ensure_started(self) -> queue.Queue:
        with self._lock:
            # A forked worker inherits the queue but not the thread.
            if self._queue is None or self._pid != os.getpid():
                self._queue = queue.Queue(self._max_pending)
                self._pid = os.getpid()
                threading.Thread(
                    target=self._run,
                    args=(self._queue,),
                    name="harness-observation-writer",
                    daemon=True,
                ).start()
            return self._queue

    def submit(self, sink: Any, write: BatchWrite, run_id: str, records: Sequence[Any]) -> bool:
        """Queue ``write(sink, records)``; return False when the record was dropped."""
        if not records:
            return True
        try:
            self._ensure_started().put_nowait((sink, write, run_id, tuple(records)))
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
                dropped = self.dropped
            if dropped == 1 or dropped % DROP_LOG_EVERY == 0:
                logger.warning(
                    "Observation queue full (%d pending); dropped %d record batch(es) so far",
                    self._max_pending,
                    dropped,
                )
            return False

    def _run(self, pending: queue.Queue) -> None:
        while True:
            items = [pending.get()]
            while len(items) < self._max_batch:
                try:
                    items.append(pending.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write(items)
            finally:
                for _ in items:
                    pending.task_done()

    @staticmethod
    def _write(items) -> None:
        # Order is preserved within a sink and run, which is all sequence
        # allocation depends on; one call per group lets stores batch a run.
        groups: dict[tuple[int, BatchWrite, str], tuple[Any, list]] = {}
        for sink, write, run_id, records in items:
            groups.setdefault((id(sink), write, run_id), (sink, []))[1].extend(records)
        for (_, write, run_id), (sink, records) in groups.items():
            try:
                write(sink, records)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Observation write failed for run %s (%d record(s)): %s",
                    run_id,
                    len(records),
                    exc,
                )

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued record is written; False on timeout."""
        pending = self._queue
        if pending is None or self._pid != os.getpid():
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        with pending.all_tasks_done:
            while pending.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                pending.all_tasks_done.wait(remaining)
        return True


_WRITER = ObservationWriter()


def observation_writer() -> ObservationWriter:
    return _WRITER


def flush_observations(timeout: float = 5.0) -> bool:
    flushed = _WRITER.flush(timeout)
    if not flushed:
        logger.warning("Observation writes still pending after %.1fs flush", timeout)
    return flushed


atexit.register(flush_observations)

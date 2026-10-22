"""One inexpensive release-file observation loop per worker, shared by all subscribers."""
import asyncio
import hashlib
from contextlib import asynccontextmanager, suppress
from pathlib import Path


class ReleaseEvents:
    def __init__(self, directory: Path, interval: float = 2.0):
        self.directory = directory
        self.interval = interval
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None
        self._revision: str | None = None

    def _read_revision(self) -> str:
        # Publication atomically replaces the index after verifying its artifacts.
        # Do not read manifests, signatures, packages, databases or user data here.
        for name in ("release-index.json", "latest.json"):
            try:
                stat = (self.directory / name).stat()
                value = (name, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                return hashlib.sha256(repr(value).encode()).hexdigest()
            except FileNotFoundError:
                continue
        return "unpublished"

    async def _observe(self):
        while True:
            try:
                revision = await asyncio.to_thread(self._read_revision)
            except OSError:
                # Temporary storage trouble must not terminate all subscriptions.
                await asyncio.sleep(self.interval)
                continue
            if revision != self._revision:
                self._revision = revision
                for queue in self._subscribers:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(revision)
            await asyncio.sleep(self.interval)

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        if self._revision is not None:
            queue.put_nowait(self._revision)
        if self._task is None:
            self._task = asyncio.create_task(self._observe())
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers and self._task is not None:
                task, self._task = self._task, None
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

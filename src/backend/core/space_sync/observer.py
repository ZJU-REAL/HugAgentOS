"""Event-driven threaded coordinator with durable retries and explicit completion barriers."""

import logging
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .journal import Journal

logger = logging.getLogger(__name__)


class DurableRegistry(FileSystemEventHandler):
    def __init__(self, root, *, settle_seconds=0.05):
        self.root = root
        self.settle = settle_seconds
        self._condition = threading.Condition()
        self._closed = False
        self._observer = None
        self._thread = None
        self.journal = Journal(root.parent / "space_sync" / "team.sqlite")
        self._operation_lock = threading.RLock()
        self._incoming = []

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._observer = Observer()
        self._observer.schedule(self, str(self.root), recursive=True)
        self._observer.start()
        try:
            self._recover()
        except Exception:
            logger.exception("Space synchronization recovery failed")
        self._thread = threading.Thread(target=self._run, name="space-sync", daemon=True)
        self._thread.start()

    def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(5)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(10)
        if not self._thread or not self._thread.is_alive():
            self._persist_incoming()
            self.journal.close()

    def enqueue(self, key, change):
        with self._condition:
            self._incoming.append((key, change, 0 if change.kind == "closed" else self.settle))
            self._condition.notify_all()

    def _persist_incoming(self):
        with self._condition:
            entries, self._incoming = self._incoming, []
        if entries:
            try:
                self.journal.put_many(entries)
            except Exception:
                with self._condition:
                    self._incoming = entries + self._incoming
                raise

    def _attempt(self, key, generation, change):
        with self._operation_lock:
            # A flush may have acknowledged it while this worker waited.
            current = self.journal.get(key)
            if current is None or current[0] != generation:
                return
            try:
                self._save(key, change)
            except Exception as exc:
                self.journal.retry(key, generation, exc)
                logger.warning(
                    "Space synchronization pending key=%s error=%s", key, type(exc).__name__
                )
                raise
            else:
                self.journal.acknowledge(key, generation)

    def _run(self):
        while not self._closed:
            try:
                self._persist_incoming()
            except Exception:
                logger.exception("Space event journal unavailable")
                with self._condition:
                    self._condition.wait(1)
                continue
            rows = self.journal.pending(ready=True)
            for key, generation, change, attempts in rows:
                if self._closed:
                    return
                try:
                    self._attempt(key, generation, change)
                except Exception:
                    pass
            try:
                self._project()
            except Exception:
                logger.exception("Space projection remains pending")
            with self._condition:
                if not self._closed:
                    if self._incoming:
                        continue
                    self._condition.wait(0.05 if self.journal.pending() else 30)

    def flush(self, scope):
        self._persist_incoming()
        failures = []
        for key, generation, change, attempts in self.journal.pending():
            if self._matches(key, scope):
                try:
                    self._attempt(key, generation, change)
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]

    def _project(self):
        pass

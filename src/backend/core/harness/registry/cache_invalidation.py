"""Named host cache invalidations invoked by domain mutations."""
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from threading import RLock


@dataclass
class CacheInvalidators:
    callbacks: dict[tuple[str, str], Callable[[], None]] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)

    def register(self, stage: str, name: str, callback: Callable[[], None]) -> None:
        with self.lock:
            self.callbacks[stage, name] = callback

    def run(self, stage: str) -> None:
        with self.lock:
            callbacks = tuple(callback for (phase, _), callback in self.callbacks.items() if phase == stage)
        for callback in callbacks:
            callback()


@lru_cache(maxsize=1)
def cache_invalidators() -> CacheInvalidators:
    """Process-owned registration state; callbacks are installed by the host."""
    return CacheInvalidators()

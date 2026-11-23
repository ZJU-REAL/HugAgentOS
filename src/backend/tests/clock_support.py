"""A controllable wall clock for TTL / lease tests.

Code under test reads ``time.time()`` through its module's ``time`` import, so a test
swaps that one reference for a clock it can move forward instead of sleeping until a
TTL lapses. Everything else on the module (``sleep``, ``monotonic`` ...) stays real.
"""

from __future__ import annotations

import time as _real_time


class FakeClock:
    """``time()`` returns a value the test moves with ``advance``; the rest is the real module."""

    def __init__(self, start: float | None = None):
        self.now = _real_time.time() if start is None else start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name):
        return getattr(_real_time, name)

    def install(self, monkeypatch, *modules) -> "FakeClock":
        """Make each module's ``time`` name (a module object or dotted path) read this clock."""
        for module in modules:
            if isinstance(module, str):
                monkeypatch.setattr(module + ".time", self)
            else:
                monkeypatch.setattr(module, "time", self)
        return self


# fakeredis stamps every command with ``time.time()`` here; expiry is judged against it.
FAKEREDIS_CLOCK_MODULE = "fakeredis._socket._base"


async def eventually(check, *, timeout: float = 5.0, interval: float = 0.05):
    """Poll ``check()`` until it returns a truthy value instead of sleeping a fixed time."""
    import asyncio

    deadline = _real_time.monotonic() + timeout
    while True:
        value = check()
        if value or _real_time.monotonic() >= deadline:
            return value
        await asyncio.sleep(interval)

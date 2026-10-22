"""Trusted owner deadlines: native stop is independent of the HTTP event loop."""

import math
import threading
import time


class NativeDeadline:
    def __init__(self, stop, *, wall_clock=time.time, monotonic=time.monotonic):
        self.stop = stop
        self.wall = wall_clock
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.closed = False
        self.fired = False
        self.expires = None
        self.maximum = None
        self.mono_expires = None
        self.mono_maximum = None
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def renew(self, expires_at, maximum=None):
        now = self.wall()
        mono = self.monotonic()
        if not math.isfinite(expires_at) or expires_at > now + 91:
            raise ValueError("invalid_deadline")
        with self.lock:
            if self.closed or self.fired:
                raise ValueError("deadline_closed")
            if maximum is not None:
                if not math.isfinite(maximum):
                    raise ValueError("invalid_maximum")
                if self.maximum is not None and maximum != self.maximum:
                    raise ValueError("maximum_changed")
                self.maximum = maximum
                if self.mono_maximum is None:
                    self.mono_maximum = mono + max(0, maximum - now)
            expiry = min(expires_at, self.maximum) if self.maximum is not None else expires_at
            self.expires = expiry
            self.mono_expires = mono + max(0, expiry - now)
            self.changed.set()

    def cancel(self):
        with self.lock:
            self.closed = True
            self.changed.set()

    def _watch(self):
        while True:
            with self.lock:
                if self.closed:
                    return
                remaining = (
                    None
                    if self.expires is None
                    else min(
                        self.expires - self.wall(),
                        self.mono_expires - self.monotonic(),
                        (
                            self.mono_maximum - self.monotonic()
                            if self.mono_maximum is not None
                            else float("inf")
                        ),
                    )
                    - 2
                )
                if remaining is not None and remaining <= 0:
                    self.fired = True
                    self.stop()  # Native process stop, not an event-loop callback.
                    return
                self.changed.clear()
            self.changed.wait(0.05 if remaining is None else max(0, min(0.05, remaining)))

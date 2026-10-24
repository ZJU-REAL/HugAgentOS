"""Merge consecutive streaming deltas so every event downstream is worth a write.

A model streams answer text, thinking and tool arguments a few characters at a
time, and each event the stream yields becomes one durable run-event append
(several Redis round trips). :class:`DeltaCoalescer` sits between the event
mapper and the consumer and merges a run of same-stream deltas into one event:

* a delta goes out at once when nothing was flushed for
  ``DELTA_FLUSH_INTERVAL_S`` (so the first token is never delayed), otherwise
  it waits in the buffer until that interval has passed or
  ``DELTA_FLUSH_CHARS`` have accumulated (see :mod:`streaming_constants`);
* only one stream is buffered at a time — a delta for a different stream, or
  any other event (tool start/end, results, errors, the end of the stream),
  flushes the buffer first, so the merged stream is the original stream in the
  original order with adjacent same-stream deltas concatenated;
* :meth:`deadline` tells a consumer that waits on its source with a timeout
  when the buffered tail is due, so text never sits behind a silent model.
"""

from __future__ import annotations

import time
from typing import Any, Hashable, List, Optional, Tuple

from core.llm.runtime import streaming_constants

Event = Tuple[str, Any]


def _stream_key(kind: str, payload: Any) -> Optional[Hashable]:
    """Which delta stream an event belongs to, or None when it is not a delta."""
    if kind in ("text_delta", "thinking_delta"):
        return kind
    if kind == "tool_call_delta":
        return (kind, payload.get("id"))
    return None


def _merge(kind: str, first: Any, text: str) -> Any:
    if kind == "tool_call_delta":
        return {**first, "delta": text}
    return text


def _text(kind: str, payload: Any) -> str:
    return (payload.get("delta") if kind == "tool_call_delta" else payload) or ""


class DeltaCoalescer:
    def __init__(self) -> None:
        self._key: Optional[Hashable] = None
        self._kind = ""
        self._first: Any = None
        self._parts: List[str] = []
        self._size = 0
        self._last_flush = float("-inf")

    def feed(self, event: Event, now: Optional[float] = None) -> List[Event]:
        """Take one mapped event; return the events to deliver now, in order."""
        kind, payload = event
        key = _stream_key(kind, payload)
        if key is None:
            return [*self.drain(now), event]
        text = _text(kind, payload)
        if not text:
            return []
        now = time.monotonic() if now is None else now
        out = self.drain(now) if key != self._key else []
        if self._key is None:
            self._key, self._kind, self._first = key, kind, payload
        self._parts.append(text)
        self._size += len(text)
        if (
            self._size >= streaming_constants.DELTA_FLUSH_CHARS
            or now - self._last_flush >= streaming_constants.DELTA_FLUSH_INTERVAL_S
        ):
            out.extend(self.drain(now))
        return out

    def drain(self, now: Optional[float] = None) -> List[Event]:
        """Flush whatever is buffered."""
        if self._key is None:
            return []
        event = (self._kind, _merge(self._kind, self._first, "".join(self._parts)))
        self._key, self._kind, self._first = None, "", None
        self._parts, self._size = [], 0
        self._last_flush = time.monotonic() if now is None else now
        return [event]

    def deadline(self) -> Optional[float]:
        """Monotonic time the buffered delta is due, or None when nothing waits."""
        if self._key is None:
            return None
        return self._last_flush + streaming_constants.DELTA_FLUSH_INTERVAL_S

    def flush_due(self, now: Optional[float] = None) -> List[Event]:
        """Flush the buffer if its deadline has passed."""
        now = time.monotonic() if now is None else now
        due = self.deadline()
        return self.drain(now) if due is not None and now >= due else []

    def reset(self) -> None:
        """Discard the buffer (the stream was abandoned) and forget the clock."""
        self._key, self._kind, self._first = None, "", None
        self._parts, self._size = [], 0
        self._last_flush = float("-inf")

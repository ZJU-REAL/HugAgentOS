"""Append-only execution events with read-only consumer payloads."""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any


def freeze_value(value: Any) -> Any:
    """Recursively detach and freeze a value before exposing it to consumers."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_value(item) for item in value)
    return copy.deepcopy(value)


def thaw_value(value: Any) -> Any:
    """Return a detached JSON-friendly copy for persistence."""
    if isinstance(value, Mapping):
        return {str(key): thaw_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [thaw_value(item) for item in sorted(value, key=str)]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class Event:
    run_id: str
    event_type: str
    phase: str
    payload: Mapping[str, Any]
    created_at: datetime
    event_seq: int | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        event_type: str,
        phase: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        return cls(
            run_id=run_id,
            event_type=event_type,
            phase=phase,
            payload=freeze_value(payload or {}),
            created_at=datetime.now(UTC),
        )


EventHandler = Callable[[Event], Awaitable[None] | None]
EventStore = Callable[[Event], Event]


class EventSink:
    """Append first, then notify observers with immutable detached events.

    Event handlers have no decision channel and never receive an execution
    object. Exceptions are isolated, so an observer cannot alter control flow.
    """

    def __init__(
        self,
        store: EventStore | None = None,
        *,
        store_timeout_ms: int = 1_000,
        subscriber_timeout_ms: int = 250,
    ) -> None:
        self._store = store
        self._store_timeout_ms = max(1, int(store_timeout_ms))
        self._subscriber_timeout_ms = max(1, int(subscriber_timeout_ms))
        self._handlers: list[EventHandler] = []
        self._memory: list[Event] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def append(self, event: Event) -> Event:
        detached = Event(
            run_id=event.run_id,
            event_type=event.event_type,
            phase=event.phase,
            payload=freeze_value(event.payload),
            created_at=event.created_at,
            event_seq=event.event_seq,
        )
        recorded = detached
        if self._store is not None:
            try:
                recorded = await asyncio.wait_for(
                    asyncio.to_thread(self._store, detached),
                    timeout=self._store_timeout_ms / 1_000,
                )
            except Exception:  # noqa: BLE001
                # Observability must not become an execution mutation/failure
                # channel. Durable stores log their own failures.
                recorded = detached
        self._memory.append(recorded)
        for handler in tuple(self._handlers):
            consumer_copy = Event(
                run_id=recorded.run_id,
                event_type=recorded.event_type,
                phase=recorded.phase,
                payload=freeze_value(recorded.payload),
                created_at=recorded.created_at,
                event_seq=recorded.event_seq,
            )
            try:

                async def notify(
                    callback: EventHandler = handler,
                    observed: Event = consumer_copy,
                ) -> None:
                    result: Any
                    if inspect.iscoroutinefunction(callback):
                        result = callback(observed)
                    else:

                        def call_handler() -> Any:
                            return callback(observed)

                        result = await asyncio.to_thread(call_handler)
                    if inspect.isawaitable(result):
                        await result

                await asyncio.wait_for(
                    notify(),
                    timeout=self._subscriber_timeout_ms / 1_000,
                )
            except Exception:  # noqa: BLE001, S112
                continue
        return recorded

    def events(self) -> tuple[Event, ...]:
        return tuple(self._memory)

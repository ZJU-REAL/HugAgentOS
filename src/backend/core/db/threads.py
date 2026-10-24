"""Run database work off the event loop.

Every query is blocking I/O, so async code never touches a ``Session`` itself:
it hands a sync function to :func:`run_db` (when it already holds a session,
such as FastAPI's request session) or to :func:`run_in_session` (which opens and
closes a session inside the worker thread). Network waits happen between those
calls, never inside them, so no pooled connection is ever held across an
``await``.

Both run on anyio's default thread limiter, which :func:`apply_thread_budget`
sizes from :func:`core.db.engine.thread_budget` — the same number the
connection pool is derived from.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
from typing import Any, Awaitable, Callable, TypeVar

import anyio.to_thread

from core.db import engine

T = TypeVar("T")


async def run_db(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run blocking database code in a worker thread."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def off_loop(fn: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """Expose a blocking function as a coroutine function that runs it in a worker thread.

    For interfaces that must be async (agent tools, startup steps, registry
    hooks) but whose body is plain database work: the wrapper keeps the
    signature and docstring, so tool schemas built from it are unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await run_db(fn, *args, **kwargs)

    return wrapper


def _in_session(fn: Callable[..., T], args: tuple, kwargs: dict) -> T:
    with engine.SessionLocal() as db:
        return fn(db, *args, **kwargs)


async def run_in_session(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``fn(db, *args, **kwargs)`` on a fresh session, closed before returning."""
    return await run_db(_in_session, fn, args, kwargs)


def _detached(db, value):
    if value is not None and value in db:
        db.refresh(value)
        db.expunge(value)
    return value


def _call_repository(db, factory, name, args, kwargs):
    return _detached(db, getattr(factory(db), name)(*args, **kwargs))


class AsyncRepository:
    """Await a session-bound repository's methods, each on a session of its own.

    ``AsyncRepository(Repo)`` exposes ``await repo.method(...)`` for every
    ``Repo(db).method(...)``; an ORM row it returns comes back loaded and
    detached, so async services read it without holding a connection.
    """

    def __init__(self, factory: Callable[[Any], Any]):
        self._factory = factory

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def call(*args: Any, **kwargs: Any) -> Any:
            return await run_in_session(_call_repository, self._factory, name, args, kwargs)

        return call


def apply_thread_budget() -> None:
    """Size this process's thread pools to the budget the connection pool assumes.

    Called once per worker on its own event loop, before it serves anything.
    """
    budget = engine.thread_budget()
    anyio.to_thread.current_default_thread_limiter().total_tokens = budget.request_threads
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=budget.background_threads, thread_name_prefix="asyncio"
        )
    )

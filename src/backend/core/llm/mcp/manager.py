"""Factory failure cleanup and run resource teardown."""
from __future__ import annotations
import asyncio
import logging
from contextvars import ContextVar
from functools import wraps
logger = logging.getLogger(__name__)

# The factory owns transient clients until it returns them to its caller.
# ContextVar values propagate into parallel HTTP probes, so partial construction
# has one cleanup owner even when cancellation interrupts asyncio.gather.
_factory_clients: ContextVar[list | None] = ContextVar("factory_mcp_clients", default=None)


def own_factory_client(client):
    clients = _factory_clients.get()
    if clients is not None:
        clients.append(client)
    return client


def close_factory_clients_on_error(function):
    @wraps(function)
    async def construct(*args, **kwargs):
        clients = []
        token = _factory_clients.set(clients)
        try:
            return await function(*args, **kwargs)
        except BaseException:
            await close_clients(list(reversed(clients)), abort=True)
            raise
        finally:
            _factory_clients.reset(token)

    return construct


async def close_clients(clients: list, *, abort=False) -> None:
    """Close every client in its owning task, deferring repeated cancellation."""
    cancelled = None
    task = asyncio.current_task()
    for client in clients:
        while True:
            before = task.cancelling() if task else 0
            try:
                await client.close(abort=True) if abort else await client.close()
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
                if task is not None and task.cancelling() > before:
                    # An external repeat-cancel interrupted this close. Finish
                    # this same client's cleanup on the same task, then the rest.
                    continue
                # A client scope itself signalled cancellation. It must not
                # prevent independent clients from releasing their resources.
                logger.debug("MCP client close signalled cancellation")
                break
            except Exception as exc:
                logger.debug("Error closing MCP client: %s", exc)
                break
    if cancelled is not None:
        raise cancelled

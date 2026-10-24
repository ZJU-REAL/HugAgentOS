"""One live SDK session: its owner task, discovered surface and call admission."""
import asyncio
import contextvars
from contextlib import AsyncExitStack, asynccontextmanager
from mcp import types
from core.llm.mcp.callbacks import route
from core.llm.mcp.client import make_client

MAX_PAGES = 100


class CallGate:
    """Read-only calls share the session; writes run alone."""

    def __init__(self):
        self._readers = 0
        self._writer = False
        self._changed = asyncio.Condition()

    @asynccontextmanager
    async def _hold(self, exclusive):
        async with self._changed:
            if exclusive:
                await self._changed.wait_for(lambda: not self._writer and not self._readers)
                self._writer = True
            else:
                await self._changed.wait_for(lambda: not self._writer)
                self._readers += 1
        try:
            yield
        finally:
            async with self._changed:
                if exclusive:
                    self._writer = False
                else:
                    self._readers -= 1
                self._changed.notify_all()

    def shared(self):
        return self._hold(False)

    def exclusive(self):
        return self._hold(True)


async def _pages(method, field):
    items, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        page = await method(cursor=cursor)
        items.extend(getattr(page, field))
        cursor = page.next_cursor
        if cursor is None:
            return items
        if cursor in seen:
            raise ValueError("MCP pagination cursor repeated")
        seen.add(cursor)
    raise ValueError(f"MCP {field} listing exceeded {MAX_PAGES} pages")


class Session:
    """Owns one SDK client from a dedicated task, so any run's task may call through it."""

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.client = None
        self.descriptors = []
        self.context = []  # (kind, descriptor) for advertised resources, templates and prompts
        self.calls = CallGate()
        self.in_flight = set()
        self.failed = False
        self._stop = asyncio.Event()
        self._owner = None

    @property
    def stopped(self):
        return self._stop.is_set() or self.failed or (self._owner is not None and self._owner.done())

    async def _sampling(self, context, params):
        call = route(self)
        if call is None:
            return types.ErrorData(code=-32600, message="No active sampling scope")
        return await call.dispatch("sampling", context, params)

    async def _elicitation(self, context, params):
        call = route(self)
        if call is None:
            return types.ElicitResult(action="decline")
        return await call.dispatch("elicitation", context, params)

    async def _discover(self, client):
        capabilities = client.server_capabilities
        if capabilities.tools is not None:
            descriptors = await _pages(client.list_tools, "tools")
            names = [d.name for d in descriptors]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate MCP tool name")
            self.descriptors = descriptors
            client.freeze(descriptors)
        listings = []
        if capabilities.resources is not None:
            listings += [("resource", client.list_resources, "resources"),
                         ("resource_template", client.list_resource_templates, "resource_templates")]
        if capabilities.prompts is not None:
            listings.append(("prompt", client.list_prompts, "prompts"))
        found = await asyncio.gather(*(_pages(method, field) for _, method, field in listings))
        self.context = [(kind, item) for (kind, _, _), items in zip(listings, found) for item in items]

    async def _own(self, client, ready):
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(client)
                self.client = client
                await self._discover(client)
                ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise

    @classmethod
    async def open(cls, name, cfg):
        session = cls(name, cfg)
        client = make_client(name, cfg, sampling_callback=session._sampling,
                             elicitation_callback=session._elicitation)
        ready = asyncio.get_running_loop().create_future()
        # A fresh context keeps run-scoped ContextVars out of the long-lived session task.
        session._owner = asyncio.create_task(session._own(client, ready), name=f"mcp-session:{name}",
                                             context=contextvars.Context())
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=float(cfg.get("discovery_timeout") or 30))
        except BaseException:
            session._stop.set()
            from core.infra.task_lifecycle import cancel_and_join
            try:
                await cancel_and_join(session._owner)
            finally:
                if ready.done() and not ready.cancelled():
                    ready.exception()
            raise
        return session

    async def close(self):
        self._stop.set()
        if self._owner is not None:
            await asyncio.shield(self._owner)

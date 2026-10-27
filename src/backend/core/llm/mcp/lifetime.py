"""A run's handle on an MCP session: its tools, its interaction authority and its release."""
import asyncio
from dataclasses import dataclass, field
from core.llm.mcp.client import _execution_timeout, is_http_cfg

MANIFEST_SOURCES = frozenset({"cloud_manifest", "plugin_manifest"})


@dataclass
class MCPResource:
    name: str
    tools: list = field(default_factory=list)
    session: object = None
    interactions: object = None
    # Write calls whose outcome is unknown in this run; exact replays are refused.
    unknown_outcomes: set = field(default_factory=set)
    closed: bool = False
    client_session_lease: tuple | None = None
    _lease: object = None
    _closing: asyncio.Task | None = None

    @property
    def descriptors(self):
        return self.session.descriptors if self.session is not None else []

    async def close(self, *, abort=False):
        """Close a run-owned session, or return a pooled one; abort also retires the pooled session."""
        # Tools reject calls at once; teardown finishes even if the caller is cancelled again.
        self.closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(abort))
        await asyncio.shield(self._closing)

    async def _close(self, abort):
        if self._lease is not None:
            await self._lease.release(abort=abort)
        elif self.session is not None:
            await self.session.close()


def _bind_tools(resource, cfg):
    from core.llm.mcp.context_tools import context_tools
    from core.llm.mcp.tools import RemoteTool
    timeout = _execution_timeout(resource.name, cfg)
    enabled, disabled = cfg.get("enable_tools"), set(cfg.get("disable_tools") or [])
    tools = [RemoteTool(resource, descriptor, timeout) for descriptor in resource.session.descriptors
             if (enabled is None or descriptor.name in enabled) and descriptor.name not in disabled]
    return tools + context_tools(resource, timeout)


async def discover(name, cfg, *, interactions=None, scope=None):
    """Bind a run to a session. Chat-scoped STDIO servers share a pooled process;
    HTTP and unscoped servers get a run-owned session."""
    if cfg.get("schema_source") in MANIFEST_SOURCES:
        from core.llm.tools.cloud_manifest import manifest_tools
        return MCPResource(name, tools=manifest_tools(name, cfg))
    if scope and not is_http_cfg(cfg):
        from core.llm.mcp.chat_sessions import acquire
        lease = await acquire(scope, name, cfg)
        resource = MCPResource(name, session=lease.session, interactions=interactions, _lease=lease)
    else:
        from core.llm.mcp.session import Session
        resource = MCPResource(name, session=await Session.open(name, cfg), interactions=interactions)
    try:
        resource.tools = _bind_tools(resource, cfg)
    except BaseException:
        await resource.close(abort=True)
        raise
    return resource

"""SDK-native connections; project authorization is attached at the transport boundary."""
import asyncio
import hashlib
import json
import math
import os
import weakref
from contextlib import asynccontextmanager
from typing import Any
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.caching import CacheConfig, InMemoryResponseCacheStore
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


HTTP_TRANSPORTS = frozenset({"streamable_http", "sse"})
_KB_SERVER_ID = "retrieve_dataset_content"


def _positive_timeout(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _execution_timeout(name: str, cfg: dict) -> float:
    """Resolve a hard tool-call deadline, with a tighter KB-specific default."""
    if cfg.get("execution_timeout") is not None:
        return _positive_timeout(cfg["execution_timeout"], 120.0)
    if name == _KB_SERVER_ID:
        return _positive_timeout(os.getenv("KB_MCP_EXECUTION_TIMEOUT_SECONDS"), 75.0)
    return _positive_timeout(os.getenv("MCP_TOOL_EXECUTION_TIMEOUT_SECONDS"), 120.0)


def _http_transport_timeout(name: str, cfg: dict, execution_timeout: float) -> float:
    if cfg.get("transport_timeout") is not None:
        return _positive_timeout(cfg["transport_timeout"], 30.0)
    if name == _KB_SERVER_ID:
        # Leave a small margin beyond the MCP server's own structured timeout
        # so callers receive its tool_result instead of a transport exception.
        return execution_timeout + 5.0
    if name == "internet_search":
        # Search returns per-query timeouts after its 45-second batch deadline.
        return 50.0
    return 30.0


def is_http_cfg(cfg: dict) -> bool:
    return cfg.get("transport") in HTTP_TRANSPORTS


@asynccontextmanager
async def authorized_transport(name, cfg):
    from core.llm.mcp.invocation import HEADER, PLUGIN_HEADER, for_url
    provider = cfg.get("oauth_provider")
    headers = cfg.get("headers") or {}
    signed = any(key.lower() == HEADER.lower() for key in headers)

    async def authorize(request):
        from core.llm.mcp.callbacks import CURRENT_CALL
        call = CURRENT_CALL.get()
        if call is not None and call.budget is not None:
            # This request is bounded by the invocation execution budget. HTTP
            # read time includes human waits; retain connect/write/pool limits.
            request.extensions["timeout"] = {**request.extensions.get("timeout", {}), "read": None}
        if provider is not None:
            await provider.mcp_request_hook(request)
        if signed:
            request.headers.update(for_url(
                cfg["url"], request.headers.get("x-current-user-id"),
                request.headers.get("x-chat-id"), request.headers.get(PLUGIN_HEADER, ""),
            ))

    timeout = _http_transport_timeout(name, cfg, _execution_timeout(name, cfg))
    if cfg["transport"] == "sse":
        def factory(headers=None, timeout=None, auth=None):
            return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                      event_hooks={"request": [authorize]})
        # One SSE stream serves all invocations; a shared idle deadline cannot
        # distinguish human waits. Discovery and invocation budgets bound work.
        async with sse_client(cfg["url"], headers=headers, timeout=timeout,
                              sse_read_timeout=math.inf, auth=provider, httpx_client_factory=factory) as streams:
            yield streams
    else:
        async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(timeout),
                                          auth=provider) as http:
            http.event_hooks["request"].append(authorize)
            async with streamable_http_client(cfg["url"], http_client=http) as streams:
                yield streams


# Listings outlive a run: each run opens its own HTTP client, so discovery results
# are shared through one store per event loop, partitioned by authorization context.
FIRST_PARTY_LISTING_TTL_MS = 60_000
_VOLATILE_HEADERS = frozenset({"x-chat-id", "x-conversation-id", "x-hugagent-invocation"})
_listing_stores = weakref.WeakKeyDictionary()


def listing_cache(cfg: dict):
    """Cache config for an HTTP server, or None to keep the SDK's per-client default."""
    if not is_http_cfg(cfg) or cfg.get("oauth_provider") is not None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    from core.llm.mcp.invocation import audience_for
    headers = {str(k).lower(): str(v) for k, v in (cfg.get("headers") or {}).items()
               if str(k).lower() not in _VOLATILE_HEADERS}
    partition = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
    store = _listing_stores.setdefault(loop, InMemoryResponseCacheStore())
    # Built-in servers send no freshness hints; their listings change only on deploy.
    ttl = FIRST_PARTY_LISTING_TTL_MS if audience_for(cfg["url"]) else 0
    return CacheConfig(store=store, partition=partition, target_id=cfg["url"], default_ttl_ms=ttl)


class AuthorizedClient(Client):
    """Reject schema changes within a frozen run instead of replaying an old permission."""
    def freeze(self, descriptors):
        self.frozen_tools = {d.name: d.model_dump(mode="json") for d in descriptors}

    async def list_tools(self, **kwargs):
        page = await super().list_tools(**kwargs)
        frozen = getattr(self, "frozen_tools", None)
        if frozen is not None:
            for tool in page.tools:
                if frozen.get(tool.name) != tool.model_dump(mode="json"):
                    raise ValueError("MCP tool schema changed; a new authorized run is required")
        return page


def make_client(name: str, cfg: dict, *, sampling_callback=None, elicitation_callback=None) -> Client:
    """One native client per authorized lifetime; negotiation and caches belong to the SDK."""
    transport = (authorized_transport(name, cfg) if is_http_cfg(cfg) else
                 StdioServerParameters(command=cfg.get("command", "python"),
                                       args=cfg.get("args", []), env=cfg.get("env") or None,
                                       cwd=cfg.get("cwd")))
    cache = listing_cache(cfg)
    # Interactive sessions also serve resources/prompts, whose SDK methods expose
    # no per-request timeout. Session.open bounds discovery, and SessionTool
    # bounds each invocation; never mutate a shared SDK timer during a callback.
    sdk_timeout = math.inf if elicitation_callback is not None else _execution_timeout(name, cfg)
    return AuthorizedClient(transport, mode="auto", read_timeout_seconds=sdk_timeout,
                            sampling_callback=sampling_callback, elicitation_callback=elicitation_callback,
                            **({"cache": cache} if cache is not None else {}))

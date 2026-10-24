"""Start trusted bundled MCPs selected by owned local plugin definitions."""

from __future__ import annotations

from urllib.parse import urlsplit



def native_bindings(configs):
    from core.config.mcp_config import mcp_http_url as _mcp_http_url
    from core.config.settings import settings
    from extensions.mcp_servers._ports import PORTS

    if not settings.deploy.is_local:
        return set()
    urls = {
        _mcp_http_url(sid).rstrip("/"): (sid, port)
        for sid, port in PORTS.items()
        if urlsplit(_mcp_http_url(sid)).hostname in ("127.0.0.1", "localhost", "::1")
    }
    return {
        urls[str(cfg.get("url") or "").rstrip("/")]
        for cfg in configs.values()
        if cfg.get("origin") == "local_plugin"
        and cfg.get("execution_scope") == "local"
        and cfg.get("owner_user_id")
        and str(cfg.get("url") or "").rstrip("/") in urls
    }


async def ensure_native_servers(configs):
    from core.infra.local_runtime.dynamic_servers import ensure_dynamic_servers
    await ensure_dynamic_servers(native_bindings(configs))

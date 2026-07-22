"""External-service connectivity probes and config utilities (shared by CE/EE).

Underlying implementation shared by `api/routes/v1/service_configs.py` (EE system console)
and `api/routes/v1/me_system.py` (CE personal system settings): connectivity testing, secret
masking, and MCP connection-pool rebuild after config changes. service_configs.py physically
does not exist in the CE derived tree, so these functions must live in core/services/
(CE-safe) and cannot remain in the EE route file.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ── Secret masking ───────────────────────────────────────────────────────────


def mask_secret(value: Optional[str]) -> Optional[str]:
    if not value or len(value) <= 8:
        return "****" if value else None
    return value[:4] + "****" + value[-4:]


def config_row_to_dict(cfg: dict, mask: bool = True) -> dict:
    """SystemConfigService row → API response dict (is_secret entries masked as needed)."""
    result = dict(cfg)
    if mask and cfg.get("is_secret") and cfg.get("config_value"):
        result["config_value"] = mask_secret(cfg["config_value"])
    return result


# ── After config changes: rebuild the MCP connection pool ────────────────────


async def reinitialize_mcp_pool(source: str = "service_configs") -> None:
    """Invalidate MCP config cache and reinitialize stable connections.

    Called after system config changes so that MCP sub-processes pick up
    new env vars (e.g. updated API URLs/keys).
    """
    try:
        from core.services.mcp_service import McpServerConfigService

        mcp_svc = McpServerConfigService.get_instance()
        mcp_svc.invalidate_cache()

        from core.llm.mcp_pool import MCPConnectionPool

        pool = MCPConnectionPool.get_instance()
        if pool.is_initialized:
            new_configs = mcp_svc.get_all_servers()
            await pool.reinitialize_if_config_changed(new_configs)
            logger.info("[%s] MCP pool reinitialize triggered after config update", source)
    except Exception as exc:
        logger.warning("[%s] MCP pool reinitialize failed: %s", source, exc)


# ── Connectivity probes ──────────────────────────────────────────────────────


async def test_http_health(base_url: str) -> dict:
    """Simple HTTP GET connectivity check."""
    url = base_url.rstrip("/")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code < 500:
            return {"success": True, "latency_ms": latency, "error": None}
        return {"success": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(exc)}


TAVILY_SEARCH_API_URL = "https://api.tavily.com/search"


async def test_tavily(api_key: str) -> dict:
    """Test Tavily API key by making a minimal search request."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TAVILY_SEARCH_API_URL,
                json={
                    "api_key": api_key,
                    "query": "test",
                    "max_results": 1,
                },
            )
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return {"success": True, "latency_ms": latency, "error": None}
        return {
            "success": False,
            "latency_ms": latency,
            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(exc)}


BAIDU_SEARCH_API_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"


async def test_baidu(api_key: str) -> dict:
    """Test Baidu AI Search API key by making a minimal search request."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                BAIDU_SEARCH_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Appbuilder-Authorization": f"Bearer {api_key}",
                },
                json={
                    "messages": [{"content": "test", "role": "user"}],
                    "search_source": "baidu_search_v2",
                    "resource_type_filter": [{"type": "web", "top_k": 1}],
                },
            )
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return {"success": True, "latency_ms": latency, "error": None}
        return {
            "success": False,
            "latency_ms": latency,
            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(exc)}


async def test_service_group(group_key: str) -> dict:
    """Run one connectivity test for a group (reading the current SystemConfigService config).

    Supports knowledge_base / industry / file_parser / internet_search; other groups return
    "unsupported". Shared by service_configs (EE) and me_system (CE).
    """
    from core.services.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()

    if group_key == "knowledge_base":
        from core.services.edition_service_probe import test_external_knowledge

        url = svc.get("knowledge_base.url")
        api_key = svc.get("knowledge_base.api_key")
        if not url:
            return {"success": False, "error": "URL 未配置", "latency_ms": 0}
        return await test_external_knowledge(url, api_key or "")

    if group_key == "industry":
        url = svc.get("industry.url")
        if not url:
            return {"success": False, "error": "URL 未配置", "latency_ms": 0}
        return await test_http_health(url)

    if group_key == "file_parser":
        url = svc.get("file_parser.api_url")
        if not url:
            return {"success": False, "error": "URL 未配置", "latency_ms": 0}
        return await test_http_health(url)

    if group_key == "internet_search":
        engine = svc.get("internet_search.engine") or "tavily"
        if engine == "baidu":
            api_key = svc.get("internet_search.baidu_api_key")
            if not api_key:
                return {"success": False, "error": "百度搜索 API Key 未配置", "latency_ms": 0}
            return await test_baidu(api_key)
        api_key = svc.get("internet_search.tavily_api_key")
        if not api_key:
            return {"success": False, "error": "Tavily API Key 未配置", "latency_ms": 0}
        return await test_tavily(api_key)

    return {"success": False, "error": "不支持的分组", "latency_ms": 0}

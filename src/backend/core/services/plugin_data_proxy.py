"""L1 data proxy: let a plugin's frontend call its upstream without holding credentials.

Before this existed, a plugin that needed the browser to fetch something had to
get a bespoke route added to the host, one per plugin.  Here the plugin
*declares* a data source in its manifest and the host exposes exactly one
generic endpoint for all of them.

The browser only ever names a source by id.  The upstream URL, the auth header
and the admin-configured credential are interpolated **here**, server-side, and
never appear in any response.

SSRF policy deliberately is not "block all private addresses": an on-prem
upstream legitimately lives on an internal IP.  Instead the admin's own
configured endpoints are the allowlist — a host the admin typed into
``admin_config`` is trusted (they chose it), and anything else must be public
HTTPS.  A plugin therefore cannot aim the proxy at cloud metadata or at an
internal service the admin never configured.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from core.infra.exceptions import AppException, BadRequestError

logger = logging.getLogger(__name__)

# ``{config.some.key}`` placeholders resolve against admin_config / SystemConfig.
_PLACEHOLDER_RE = re.compile(r"\{config\.([A-Za-z0-9_.\-]{1,120})\}")

MAX_RESPONSE_BYTES_CEILING = 8 * 1024 * 1024

# One connection pool for all proxy calls (per-request timeouts are passed at
# call time). Building an AsyncClient per request would redo the TCP+TLS
# handshake to the same upstream every time.
_client: Optional[httpx.AsyncClient] = None


def _shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


class PluginDataSourceError(AppException):
    """A safe, user-facing failure from a plugin-declared upstream call."""

    def __init__(self, message: str, *, status_code: int = 502, retryable: bool = False):
        super().__init__(code=52040, message=message, status_code=status_code, data={"retryable": retryable})


# ── Credential interpolation ─────────────────────────────────────────────────

def _config_value(key: str) -> str:
    from core.services.system_config import SystemConfigService

    try:
        return str(SystemConfigService.get_instance().get(key) or "").strip()
    except Exception:  # pragma: no cover - config backend unavailable
        logger.warning("plugin_data_proxy: 读取系统配置失败 key=%s", key)
        return ""


def _interpolate(template: str) -> Tuple[str, List[str]]:
    """Fill ``{config.x}`` placeholders; returns (text, missing_keys)."""
    missing: List[str] = []

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1)
        value = _config_value(key)
        if not value:
            missing.append(key)
        return value

    return _PLACEHOLDER_RE.sub(repl, template), missing


def _admin_config_field_keys(slug: str) -> List[str]:
    """The plugin's declared admin_config field keys, read from plugin.json only.

    ``_admin_config_for_slug`` runs the full bundle normalization (walking the
    skills directory) — far too heavy for a per-request path. This mirrors the
    lightweight pattern of ``plugin_service._has_admin_config_for_slug``.
    """
    from core.services.plugin_importer import _ext_or_top, manifest_extensions
    from core.services.plugin_service import _resolve_plugin_dir

    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is None:
        return []
    try:
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    admin_config = _ext_or_top(manifest, manifest_extensions(manifest), "admin_config")
    if not isinstance(admin_config, dict):
        return []
    return [
        str(field.get("key") or "")
        for field in admin_config.get("fields") or []
        if isinstance(field, dict)
    ]


def _configured_hosts(slug: str) -> set:
    """Hosts the admin explicitly configured for this plugin (the SSRF allowlist)."""
    hosts = set()
    for key in _admin_config_field_keys(slug):
        raw = _config_value(key)
        if not raw:
            continue
        host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
        if host:
            hosts.add(host.lower())
    return hosts


async def _is_private_address(host: str) -> bool:
    """True when the host resolves only to loopback / private / link-local space."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        pass
    try:
        # Off the event loop: a slow resolver must not stall every other request.
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        # Unresolvable: treat as unsafe rather than letting the request out.
        return True
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved):
            return False
    return True


async def _check_target(url: str, slug: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PluginDataSourceError("插件数据源地址非法", status_code=400)
    host = parts.hostname.lower()
    if host in _configured_hosts(slug):
        # Admin-configured endpoint: internal addresses are legitimate here.
        return
    if parts.scheme != "https":
        raise PluginDataSourceError("插件数据源必须使用 HTTPS，或指向管理员已配置的地址", status_code=400)
    if await _is_private_address(host):
        raise PluginDataSourceError("插件数据源不得指向内网地址", status_code=400)


# ── Parameter validation ─────────────────────────────────────────────────────

def validate_params(schema: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce and bound-check caller params against the manifest's declaration.

    Anything the schema does not name is discarded, so a caller cannot smuggle
    extra fields into the upstream request.
    """
    out: Dict[str, Any] = {}
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        ptype = spec.get("type", "string")
        present = key in raw and raw[key] is not None
        value = raw.get(key) if present else spec.get("default")
        if value is None:
            if spec.get("required"):
                raise BadRequestError(message=f"缺少必填参数：{key}")
            continue
        try:
            if ptype == "integer":
                value = int(value)
            elif ptype == "number":
                value = float(value)
            elif ptype == "boolean":
                value = bool(value)
            elif ptype == "array":
                if not isinstance(value, list):
                    raise ValueError
                value = [str(v)[:200] for v in value[:100]]
            else:
                value = str(value)[: int(spec.get("max_length") or 2000)]
        except (TypeError, ValueError):
            raise BadRequestError(message=f"参数类型不正确：{key}")
        if ptype in ("integer", "number"):
            if "min" in spec and value < spec["min"]:
                value = spec["min"]
            if "max" in spec and value > spec["max"]:
                value = spec["max"]
        out[key] = value
    missing = [k for k, s in (schema or {}).items() if isinstance(s, dict) and s.get("required") and k not in out]
    if missing:
        raise BadRequestError(message=f"缺少必填参数：{'、'.join(missing)}")
    return out


# ── Execution ────────────────────────────────────────────────────────────────

async def _call_provider(provider: str, params: Dict[str, Any], *, slug: str) -> Any:
    """Dispatch to an in-process data provider published by a host module.

    The contract already constrains the reference to ``mcp_servers.*:name``;
    on top of that, only callables the module deliberately lists in its
    ``DATA_PROVIDERS`` dict are reachable — a manifest cannot invoke arbitrary
    importable functions.
    """
    import importlib

    module_path, _, name = provider.partition(":")
    if not module_path.startswith("mcp_servers.") or not name:
        raise PluginDataSourceError("插件数据源 provider 引用非法", status_code=400)
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        logger.warning("plugin_data_proxy: slug=%s provider 模块不存在 %s", slug, module_path)
        raise PluginDataSourceError("插件数据源在当前部署中不可用", status_code=503)
    registry = getattr(module, "DATA_PROVIDERS", None)
    handler = registry.get(name) if isinstance(registry, dict) else None
    if not callable(handler):
        logger.warning("plugin_data_proxy: slug=%s provider 未注册 %s", slug, provider)
        raise PluginDataSourceError("插件数据源在当前部署中不可用", status_code=503)
    try:
        return await handler(params)
    except AppException:
        # Providers raise user-safe domain errors (config missing, upstream
        # auth failed…) — pass them through untouched.
        raise
    except Exception as exc:  # noqa: BLE001 - provider bugs must not 500 raw
        logger.exception("plugin_data_proxy: slug=%s provider=%s 执行失败 %s", slug, provider, exc)
        raise PluginDataSourceError("插件数据源处理失败", retryable=True)


async def call_data_source(
    source: Dict[str, Any], params: Dict[str, Any], *, slug: str
) -> Any:
    """Run one declared data source and return its decoded business payload."""
    provider = str(source.get("provider") or "")
    if provider:
        safe_params = validate_params(source.get("params_schema") or {}, params)
        return await _call_provider(provider, safe_params, slug=slug)

    url_template = str(source.get("url") or "")
    url, missing = _interpolate(url_template)
    if missing:
        raise PluginDataSourceError(
            f"插件所需的系统配置尚未填写：{'、'.join(sorted(set(missing)))}", status_code=503
        )
    await _check_target(url, slug)

    headers = {"Accept": "application/json"}
    auth = source.get("auth") or {}
    if auth.get("header"):
        value, auth_missing = _interpolate(str(auth.get("value") or ""))
        if auth_missing:
            raise PluginDataSourceError(
                f"插件所需的访问凭据尚未配置：{'、'.join(sorted(set(auth_missing)))}", status_code=503
            )
        if value:
            headers[str(auth["header"])] = value

    method = str(source.get("method") or "POST").upper()
    timeout = float(source.get("timeout_ms") or 10_000) / 1000.0
    max_bytes = min(int(source.get("max_bytes") or 1_048_576), MAX_RESPONSE_BYTES_CEILING)
    safe_params = validate_params(source.get("params_schema") or {}, params)

    try:
        client = _shared_client()
        if method == "GET":
            response = await client.get(url, params=safe_params, headers=headers, timeout=timeout)
        else:
            response = await client.post(url, json=safe_params, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        raise PluginDataSourceError("插件数据源请求超时", retryable=True)
    except httpx.HTTPError as exc:
        logger.warning("plugin_data_proxy: slug=%s source=%s 请求失败 %s", slug, source.get("id"), exc)
        raise PluginDataSourceError("插件数据源请求失败", retryable=True)

    if response.status_code >= 400:
        # Upstream detail is logged, not echoed: it can carry credentials or
        # internal hostnames.
        logger.warning(
            "plugin_data_proxy: slug=%s source=%s 上游返回 %s",
            slug, source.get("id"), response.status_code,
        )
        raise PluginDataSourceError(
            f"插件数据源返回错误（{response.status_code}）",
            retryable=response.status_code >= 500,
        )

    body = response.content[: max_bytes + 1]
    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes]
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        text = body.decode("utf-8", errors="replace")
        return {"text": text, **({"truncated": True} if truncated else {})}


def module_asset_path(slug: str, entry_or_path: str) -> Optional["object"]:
    """Resolve a plugin-relative web asset path to a real file inside the bundle.

    Returns None when the path escapes the plugin's ``web/`` directory or does
    not exist — plugin UI assets live in the plugin package, never in the host
    frontend tree, so this is the only door to them.
    """
    from pathlib import Path

    from core.services.plugin_service import _resolve_plugin_dir

    plugin_dir = _resolve_plugin_dir(slug)
    if plugin_dir is None:
        return None
    web_root = (plugin_dir / "web").resolve()
    if not web_root.is_dir():
        return None
    relative = (entry_or_path or "").replace("\\", "/").lstrip("/")
    if relative.startswith("web/"):
        relative = relative[len("web/"):]
    candidate = (web_root / relative).resolve()
    try:
        candidate.relative_to(web_root)
    except ValueError:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None

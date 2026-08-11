"""Shared MCP mutation helpers used by admin and self-service routes."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from typing import Any, Dict, Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from core.db.models import AdminMcpServer
from core.infra.crypto import decrypt_secret, encrypt_secret
from core.infra.exceptions import BadRequestError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
_PROBE_TIMEOUT_S = 10.0
_ENC_PREFIX = "enc:v1:"
_RUNTIME_SECRET_PREFIX = "__hugagent_runtime_secret__"
_URL_OVERRIDE_STORAGE_KEY = f"{_RUNTIME_SECRET_PREFIX}url"
_QUERY_SECRET_STORAGE_PREFIX = f"{_RUNTIME_SECRET_PREFIX}query__"
_OAUTH_BUNDLE_STORAGE_KEY = f"{_RUNTIME_SECRET_PREFIX}oauth_bundle"
# Keep these prefixes narrow: matching all ULA addresses would let split-horizon
# private DNS answers reach the public-DoH fallback intended only for fake-IP DNS.
_SYNTHETIC_DNS_PROXY_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fc00::/18"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)
_PUBLIC_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

_HIGH_RISK_TERMS = {
    "delete",
    "drop",
    "remove",
    "destroy",
    "execute",
    "shell",
    "command",
    "payment",
    "transfer",
    "send_email",
    "send_message",
    "publish",
    "admin",
}
_MEDIUM_RISK_TERMS = {
    "create",
    "update",
    "write",
    "edit",
    "upload",
    "post",
    "insert",
    "invite",
    "approve",
}


def encrypt_mcp_headers(headers: Dict[str, str] | None) -> Dict[str, str]:
    """Encrypt MCP header values at rest while preserving header names for runtime use."""
    encrypted: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        raw = str(value)
        encrypted[normalized_key] = (
            raw if raw.startswith(_ENC_PREFIX) else _ENC_PREFIX + encrypt_secret(raw)
        )
    return encrypted


def decrypt_mcp_headers(headers: Dict[str, str] | None) -> Dict[str, str]:
    """Decrypt encrypted MCP headers; legacy plaintext rows remain readable."""
    plaintext: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        raw = str(value)
        if raw.startswith(_ENC_PREFIX):
            decrypted = decrypt_secret(raw[len(_ENC_PREFIX) :])
            if decrypted is not None:
                plaintext[str(key)] = decrypted
        else:
            plaintext[str(key)] = raw
    return plaintext


def mcp_query_secret_storage_key(name: str) -> str:
    """Return the encrypted-header storage key for a runtime URL query value."""
    return f"{_QUERY_SECRET_STORAGE_PREFIX}{str(name).strip()}"


def mcp_url_override_storage_key() -> str:
    """Return the encrypted-header storage key for a per-install MCP endpoint."""
    return _URL_OVERRIDE_STORAGE_KEY


def mcp_oauth_bundle_storage_key() -> str:
    """Encrypted-header key holding the OAuth token/client metadata bundle."""
    return _OAUTH_BUNDLE_STORAGE_KEY


def is_internal_mcp_secret_key(key: str) -> bool:
    """Whether a JSON header key is internal storage and must never be sent as HTTP."""
    return str(key).startswith(_RUNTIME_SECRET_PREFIX)


def materialize_mcp_http_connection(
    url: str,
    stored_headers: Dict[str, str] | None,
) -> tuple[str, Dict[str, str]]:
    """Build the runtime URL/headers without persisting query or URL secrets in plaintext.

    ``AdminMcpServer.headers`` is already encrypted at rest and doubles as the
    encrypted secret envelope for credentials that providers require in the URL.
    Reserved keys are removed before the HTTP request is created.
    """
    values = {str(key): str(value) for key, value in (stored_headers or {}).items()}
    runtime_url = values.pop(_URL_OVERRIDE_STORAGE_KEY, "").strip() or (url or "").strip()
    query_secrets: Dict[str, str] = {}
    runtime_headers: Dict[str, str] = {}
    for key, value in values.items():
        if key.startswith(_QUERY_SECRET_STORAGE_PREFIX):
            query_name = key[len(_QUERY_SECRET_STORAGE_PREFIX) :].strip()
            if query_name:
                query_secrets[query_name] = value
        elif not is_internal_mcp_secret_key(key):
            runtime_headers[key] = value

    if query_secrets:
        parsed = urlparse(runtime_url)
        existing = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in query_secrets
        ]
        existing.extend(query_secrets.items())
        runtime_url = urlunparse(parsed._replace(query=urlencode(existing)))
    return runtime_url, runtime_headers


def mask_mcp_headers(headers: Dict[str, str] | None) -> Dict[str, str]:
    """Return only masked header values for admin/user API responses."""
    return {str(key): "***" for key in (headers or {}) if not is_internal_mcp_secret_key(str(key))}


def encrypt_legacy_mcp_headers(db: Session) -> int:
    """One-way startup migration for pre-encryption MCP header rows."""
    changed = 0
    for row in db.query(AdminMcpServer).filter(AdminMcpServer.headers.isnot(None)).all():
        headers = dict(row.headers or {})
        if not headers or all(str(value).startswith(_ENC_PREFIX) for value in headers.values()):
            continue
        row.headers = encrypt_mcp_headers(headers)
        changed += 1
    if changed:
        db.commit()
    return changed


def auth_schema_from_headers(headers: Dict[str, str] | None) -> List[Dict[str, Any]]:
    """Describe required install-time headers without copying any credential value."""
    return [
        {
            "key": str(key),
            "label": str(key),
            "target": "header",
            "required": True,
            "secret": True,
        }
        for key in sorted((headers or {}).keys(), key=str.lower)
        if str(key).strip() and not is_internal_mcp_secret_key(str(key))
    ]


def tool_snapshot_hash(tools: Iterable[Dict[str, Any]] | None) -> str:
    """Stable digest used to detect remote MCP tool/schema drift."""
    normalized = sorted(
        [
            {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "inputSchema": item.get("inputSchema") or {},
            }
            for item in (tools or [])
        ],
        key=lambda item: item["name"],
    )
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assess_mcp_risk(tools: Iterable[Dict[str, Any]] | None) -> tuple[str, Dict[str, Any]]:
    """Classify a reviewed tool snapshot and explain which tools raised the level."""
    high: List[str] = []
    medium: List[str] = []
    for item in tools or []:
        name = str(item.get("name") or "")
        haystack = f"{name} {item.get('description') or ''}".lower().replace("-", "_")
        if any(term in haystack for term in _HIGH_RISK_TERMS):
            high.append(name)
        elif any(term in haystack for term in _MEDIUM_RISK_TERMS):
            medium.append(name)
    level = "high" if high else "medium" if medium else "low"
    return level, {
        "high_risk_tools": sorted(set(high)),
        "medium_risk_tools": sorted(set(medium)),
        "requires_confirmation": bool(high),
    }


def _is_forbidden_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_synthetic_dns_proxy_ip(address: str) -> bool:
    """Return whether an address belongs to a known TUN fake-IP DNS range."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(
        ip.version == network.version and ip in network for network in _SYNTHETIC_DNS_PROXY_NETWORKS
    )


async def _resolve_public_dns_via_doh(hostname: str) -> set[str]:
    """Resolve a fake-IP hostname through fixed public DoH endpoints.

    Docker Desktop and TUN proxies commonly synthesize answers from dedicated
    IPv4 or IPv6 ranges.
    Treating those answers as ordinary reserved addresses hides every legitimate
    remote MCP, while blindly allowing them weakens SSRF protection. A fixed DoH
    recheck recovers the provider's real A/AAAA records before allowing the URL.
    """
    for endpoint in _PUBLIC_DOH_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
                responses = await asyncio.gather(
                    *(
                        client.get(
                            endpoint,
                            params={"name": hostname, "type": record_type},
                            headers={"Accept": "application/dns-json"},
                        )
                        for record_type in ("A", "AAAA")
                    )
                )
            addresses: set[str] = set()
            for response in responses:
                response.raise_for_status()
                payload = response.json()
                if int(payload.get("Status", -1)) != 0:
                    continue
                for answer in payload.get("Answer") or []:
                    if int(answer.get("type", 0)) not in {1, 28}:
                        continue
                    value = str(answer.get("data") or "").strip().rstrip(".")
                    try:
                        addresses.add(str(ipaddress.ip_address(value)))
                    except ValueError:
                        continue
            if addresses:
                return addresses
        except Exception as exc:  # noqa: BLE001 - try the next fixed resolver
            logger.warning("Public DNS recheck failed via %s: %s", endpoint, type(exc).__name__)
    return set()


async def validate_remote_mcp_url(
    url: str,
    *,
    allow_private_network: bool = False,
    require_https: bool = True,
) -> None:
    """Validate a remote MCP URL before every user submission/market install.

    Resolution is checked as well as the literal host so public-looking DNS names
    cannot point at loopback, RFC1918, link-local, or cloud metadata addresses.
    Admin-only publishing may explicitly allow private network targets.
    """
    value = (url or "").strip()
    parsed = urlparse(value)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes:
        scheme_label = "HTTPS" if require_https else "HTTP/HTTPS"
        raise BadRequestError(message=f"MCP 服务地址必须使用 {scheme_label}")
    if not parsed.hostname or parsed.username or parsed.password:
        raise BadRequestError(message="MCP 服务地址格式无效，且不能包含 URL 用户凭据")
    if (
        parsed.hostname.lower() in {"localhost", "localhost.localdomain"}
        and not allow_private_network
    ):
        raise BadRequestError(message="MCP 服务地址不能指向本机或内网")
    if allow_private_network:
        return
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and _is_forbidden_ip(str(literal)):
        raise BadRequestError(message="MCP 服务地址不能指向本机、内网或保留地址")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM),
            timeout=3.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(message=f"MCP 服务域名解析失败：{type(exc).__name__}") from exc
    addresses = {str(info[4][0]) for info in infos if info and info[4]}
    if not addresses:
        raise BadRequestError(message="MCP 服务域名解析到了本机、内网或保留地址")
    synthetic_addresses = {address for address in addresses if _is_synthetic_dns_proxy_ip(address)}
    if any(
        _is_forbidden_ip(address) and address not in synthetic_addresses for address in addresses
    ):
        raise BadRequestError(message="MCP 服务域名解析到了本机、内网或保留地址")
    if synthetic_addresses:
        public_addresses = await _resolve_public_dns_via_doh(parsed.hostname)
        if not public_addresses or any(_is_forbidden_ip(address) for address in public_addresses):
            raise BadRequestError(message="MCP 服务域名公共 DNS 安全复核失败")


def refresh_mcp_caches() -> None:
    """Invalidate MCP, catalog, capability, and prompt caches after a mutation."""
    invalidators = []
    try:
        from core.services.mcp_service import McpServerConfigService

        invalidators.append(McpServerConfigService.get_instance().invalidate_cache)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to load the MCP cache invalidator: %s", exc)

    for module_name, function_name in (
        ("core.config.catalog_loader", "invalidate_catalog_cache"),
        ("core.config.catalog_runtime", "invalidate_runtime_catalog_cache"),
        ("core.config.catalog_resolver", "invalidate_capability_cache"),
        ("prompts.prompt_runtime", "invalidate_prompt_cache"),
    ):
        try:
            module = __import__(module_name, fromlist=[function_name])
            invalidators.append(getattr(module, function_name))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unable to load cache invalidator %s.%s: %s", module_name, function_name, exc
            )

    for invalidate in invalidators:
        try:
            invalidate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP cache invalidation failed: %s", exc)


async def probe_mcp_connectivity(
    row: AdminMcpServer,
    db: Session | None = None,
    oauth_provider: Any = None,
    timeout_seconds: float = _PROBE_TIMEOUT_S,
) -> tuple[bool, str]:
    """Return whether an MCP server can connect and enumerate its tools."""
    from core.services.mcp_service import McpServerConfigService

    cfg = McpServerConfigService.get_instance()._row_to_config(row)
    if oauth_provider is not None:
        cfg["oauth_provider"] = oauth_provider

    async def _do_probe() -> None:
        from core.llm.mcp_pool import make_client

        if row.transport not in ("stdio", "streamable_http", "sse"):
            raise RuntimeError(f"Unknown transport: {row.transport}")
        is_http = row.transport in ("streamable_http", "sse")
        # AgentScope's stateful HTTP MCP client owns an AnyIO cancel scope that
        # is bound to the task which opened it.  Failed handshakes (notably 401)
        # can finalize the underlying async generator in another task, masking
        # the real HTTP error with "Attempted to exit cancel scope...".  A
        # stateless client keeps the complete list_tools lifecycle in this task.
        client = make_client(row.server_id, cfg, is_stateful=not is_http)
        try:
            if not is_http:
                await client.connect()
            discovered = await client.list_tools()
            tools_meta = []
            for tool in discovered or []:
                input_schema = (
                    getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
                )
                if hasattr(input_schema, "model_dump"):
                    input_schema = input_schema.model_dump(mode="json")
                tools_meta.append(
                    {
                        "name": tool.name,
                        "description": getattr(tool, "description", "") or "",
                        "inputSchema": input_schema,
                    }
                )
            row.tools_json = tools_meta
            if db is not None and tools_meta:
                from core.ontology.build_validator import OntologyBuildValidator

                extra_config = row.extra_config or {}
                ontology_tags = {
                    str(tag).strip()
                    for tag in (extra_config.get("ontology_tags") or [])
                    if str(tag).strip()
                }
                tool_tags = extra_config.get("tool_tags")
                if isinstance(tool_tags, dict):
                    ontology_tags.update(
                        str(tag).strip()
                        for tags in tool_tags.values()
                        if isinstance(tags, list)
                        for tag in tags
                        if str(tag).strip()
                    )
                report = OntologyBuildValidator(db).validate(
                    asset_type="tool",
                    name=row.display_name,
                    description=row.description or "",
                    tool_names=[item["name"] for item in tools_meta],
                    tool_schemas={item["name"]: item["inputSchema"] for item in tools_meta},
                    ontology_tags=sorted(ontology_tags),
                )
                if not report.valid:
                    messages = "; ".join(item.message for item in report.errors)
                    raise RuntimeError(f"Domain Pack 构建校验失败：{messages}")
        finally:
            # Stateless HTTP list_tools opens and closes its own connection.
            # Calling close() is unnecessary and can re-enter the SDK's
            # task-bound cancel scope.  Stateful stdio still requires cleanup.
            if not is_http:
                try:
                    await client.close()
                except BaseException as exc:  # noqa: BLE001
                    logger.debug("MCP probe client close failed: %s", exc)

    try:
        await asyncio.wait_for(_do_probe(), timeout=timeout_seconds)
        return True, ""
    except asyncio.TimeoutError:
        return False, f"连接超时（> {timeout_seconds:.0f}s）"
    except BaseException as exc:
        current_task = asyncio.current_task()
        if (
            isinstance(exc, asyncio.CancelledError)
            and current_task is not None
            and current_task.cancelling() > 0
        ):
            raise
        return False, _format_mcp_probe_exception(exc)


def _format_mcp_probe_exception(exc: BaseException) -> str:
    """Return the actionable leaf error without leaking AnyIO cleanup noise."""
    queue = [exc]
    fallback = ""
    while queue:
        current = queue.pop(0)
        nested = getattr(current, "exceptions", None)
        if nested:
            queue[0:0] = list(nested)
            continue

        message = str(current).strip()
        lowered = message.lower()
        is_cancel_noise = isinstance(current, (asyncio.CancelledError, GeneratorExit)) or any(
            marker in lowered
            for marker in (
                "cancel scope",
                "cancelled via",
                "canceled via",
                "generator didn't stop",
            )
        )
        if is_cancel_noise:
            continue

        if isinstance(current, httpx.HTTPStatusError) and current.response is not None:
            status = current.response.status_code
            reason = current.response.reason_phrase
            if status in (401, 403):
                return f"远端 MCP 返回 {status} {reason}，请检查鉴权设置和 Token"
            return f"远端 MCP 返回 HTTP {status} {reason}"
        if isinstance(current, httpx.ConnectError):
            return "无法连接远端 MCP，请检查地址、网络和服务状态"
        if message:
            return f"{type(current).__name__}: {message}"
        fallback = fallback or type(current).__name__
    return fallback or "MCP 连接被中断，请稍后重试"

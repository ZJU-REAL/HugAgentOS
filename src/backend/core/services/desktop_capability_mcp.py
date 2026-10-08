"""Cloud connector manifests, authorization and MCP invocation."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer
from core.services.desktop_capability_configs import _user_capability_configs
from core.services.desktop_capability_credentials import _secrets_from_config
from core.services.desktop_capability_protocol import (
    CapabilityManifestStaleError,
    build_manifest,
    canonical_hash,
    public_tool_schemas,
)
from core.services.desktop_capability_security import guard_capability_content


def build_user_capability_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    """构建当前用户的云端能力 manifest（server 级 + 完整脱敏 schema）。

    清单是账号**拥有**的连接器，云端关着的也在里面（带 ``enabled=false``）：装了
    什么由云端定，开不开由本机定。只收 ``streamable_http`` 传输的 server——网关按
    MCP streamable-http 协议透明反代；stdio / sse 传输的（本就极少）不进桌面清单。
    凭据（URL 内嵌密钥、headers、OAuth）一律留在云端连接层，manifest 不携带任何密钥。
    """
    from core.config.catalog_runtime import _DEFAULT_MCP_ICONS

    keys, enabled_keys, all_cfgs = _user_capability_configs(user_id, use_cache=use_cache)
    enabled = set(enabled_keys)

    meta: Dict[str, AdminMcpServer] = {}
    if keys:
        with SessionLocal() as db:
            rows = db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(keys)).all()
            meta = {r.server_id: r for r in rows}
            db.expunge_all()

    servers: List[Dict[str, Any]] = []
    for sid in keys:
        cfg = all_cfgs.get(sid) or {}
        if cfg.get("transport") != "streamable_http":
            continue
        row = meta.get(sid)
        source_plugin = row.source_plugin if row else None
        raw_tools = row.tools_json if row else None
        tools = public_tool_schemas(raw_tools)
        servers.append(
            {
                "server_id": sid,
                "display_name": (row.display_name if row else None) or sid,
                "description": (row.description if row else None) or "",
                "created_at": row.created_at.isoformat() if row and row.created_at else None,
                "source_plugin": source_plugin,
                "origin": "cloud",
                "execution_scope": "cloud",
                "tools": tools,
                "schema_hash": canonical_hash(tools),
                # 云端此刻的启停，只作本机首次落地的初值。
                "enabled": sid in enabled,
                # 图标随条目走：本机没有云端那张内置图标表，也读不到库里的自定义图标，
                # 不带下去就是网页端有图、桌面端一片空白。
                "icon": (row.icon if row else "") or _DEFAULT_MCP_ICONS.get(sid, ""),
            }
        )
    # The revision intentionally excludes credentials, URLs and timestamps.
    return build_manifest(servers)


def resolve_gateway_target(user_id: str, server_id: str, *, fresh: bool = False) -> Optional[dict]:
    """网关调用前的授权解析：server 必须是该用户拥有的。

    按拥有集而不是云端启用集裁决：启停已经交给本机，用户在桌面端打开的连接器
    必须真的调得通。管理员停用的连接器不在拥有集里，这条边界没有放宽。

    命中返回**已物化**（含云端侧凭据/headers、URL 已去尾斜杠）的连接配置——
    只在云端进程内使用，绝不回传桌面。未命中 / 非 streamable_http / 无 URL
    一律返回 None（调用方 404，不区分“不存在/无权”）。
    """
    keys, _enabled, all_cfgs = _user_capability_configs(user_id, use_cache=not fresh)
    if server_id not in keys:
        return None
    target = all_cfgs.get(server_id)
    if not isinstance(target, dict) or target.get("transport") != "streamable_http":
        return None
    url = (target.get("url") or "").rstrip("/")
    if not url:
        return None
    target = dict(target)
    target["url"] = url
    return target


def resolve_gateway_tool(
    user_id: str,
    server_id: str,
    tool_name: str,
    *,
    schema_hash: str,
) -> Optional[dict]:
    """Resolve one currently-authorized tool and its private cloud target.

    The desktop's cached schema is discovery data, never an authorization
    grant. Every invocation rechecks both server visibility and the current
    DB tool allowlist before any upstream connection is opened.
    """
    target = resolve_gateway_target(user_id, server_id, fresh=True)
    wanted = str(tool_name or "").strip()
    if target is None or not wanted:
        return None
    with SessionLocal() as db:
        row = db.get(AdminMcpServer, server_id)
        raw_tools = row.tools_json if row is not None else None
    tools = public_tool_schemas(raw_tools)
    if canonical_hash(tools) != str(schema_hash or ""):
        raise CapabilityManifestStaleError("capability manifest changed")
    for tool in tools:
        if tool["name"] == wanted:
            return {
                "user_id": str(user_id),
                "server_id": str(server_id),
                "target": target,
                "tool": tool,
            }
    return None


async def invoke_gateway_tool(
    resolved: Dict[str, Any],
    arguments: Dict[str, Any],
    runtime_headers: Dict[str, str],
) -> Dict[str, Any]:
    """Execute one MCP tool inside the cloud network and return a ToolChunk.

    This deliberately terminates the desktop-facing hop as ordinary JSON. The
    cloud process still uses the native MCP client directly against the private
    target, preserving OAuth, upstream credentials and MCP result conversion
    without extending an MCP SSE session across the public gateway.
    """
    import mcp.types
    from core.llm.mcp_pool import make_client

    arguments = dict(arguments or {})
    from core.services.automation_remote_effect import RECEIPT_TOOLS, bind_remote_effect

    tool_name = str(resolved["tool"]["name"])
    if tool_name in RECEIPT_TOOLS and arguments.get("tool_effect_id"):
        operation_id = arguments.pop("tool_effect_id")
        arguments["tool_effect_id"] = await asyncio.to_thread(
            bind_remote_effect,
            str(resolved["user_id"]),
            str(resolved["server_id"]),
            tool_name,
            operation_id,
            arguments,
        )

    target = dict(resolved["target"])
    upstream_headers = {
        str(k).lower(): str(v)
        for k, v in (runtime_headers or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    # Cloud-owned credentials override every desktop-supplied header. Identity
    # is bound to the verified capability token, never to a client header.
    for key, value in dict(target.get("headers") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            upstream_headers[key.lower()] = value
    upstream_headers["x-current-user-id"] = str(resolved["user_id"])
    upstream_headers["accept-encoding"] = "identity"
    # The authenticated cloud gateway is a new invocation hop. Never forward
    # a device-issued proof to an internal MCP that trusts the cloud key.
    from core.llm.mcp_invocation import HEADER, PLUGIN_HEADER, for_url

    upstream_headers.pop(HEADER.lower(), None)
    upstream_headers.pop(PLUGIN_HEADER.lower(), None)
    upstream_headers.update(for_url(
        target.get("url"), str(resolved["user_id"]), upstream_headers.get("x-chat-id", "")
    ))
    target["headers"] = upstream_headers

    client = make_client(str(resolved["server_id"]), target, is_stateful=False)
    raw_tool = mcp.types.Tool.model_validate(resolved["tool"])
    # Skip a second tools/list call in the cloud: the allowlisted schema was read
    # from the same DB row immediately above. get_tool then performs only the
    # real initialize + tools/call lifecycle against the private MCP target.
    client._cached_tools = [raw_tool]  # noqa: SLF001 - AgentScope has no public preload API
    tool = await client.get_tool(raw_tool.name)
    timeout = max(1.0, float(client.execution_timeout or 120.0)) + 10.0
    chunk = await asyncio.wait_for(tool(**dict(arguments or {})), timeout=timeout)
    chunk.metadata.setdefault("origin", "cloud")
    chunk.metadata.setdefault("mcp_server_id", str(resolved["server_id"]))
    return guard_capability_content(
        str(resolved["user_id"]),
        chunk.model_dump(mode="json"),
        extra_secrets=_secrets_from_config(target),
    )

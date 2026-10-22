"""managed connectors responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List
from features.desktop.auth import bridge as bridge_auth
import logging
logger = logging.getLogger(__name__)
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity

def account_profile(st: Dict[str, Any]) -> str:
    """Profile label of the bridged account (opaque tokens get a deterministic label)."""
    from core.capabilities.reference import profile_id
    from core.capabilities.manifest_protocol import token_subject

    return profile_id(
        str(st["cloud_base"]), token_subject(str(st.get("token") or "")) or "opaque-subject"
    )


def project_managed_profile(st: Dict[str, Any], manifest: Dict[str, Any]) -> None:
    """Write the account's cloud connector references into mcp.json (managedProfiles)."""
    from features.desktop.capabilities import mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled
    from core.capabilities.reference import cloud_issuer

    if not capabilities_enabled():
        return
    try:
        mcp_json.project_managed_profile(
            account_profile(st),
            cloud_instance_id=cloud_issuer(str(st["cloud_base"])),
            catalog_revision=str(manifest["revision"]),
            servers=list(manifest.get("servers") or []),
            initial_enabled={
                str(s["server_id"]): bool(s.get("enabled", True))
                for s in manifest.get("servers") or []
                if isinstance(s, dict) and s.get("server_id")
            },
        )
    except mcp_json.McpJsonCorrupt as exc:
        logger.error("[cloud-bridge] mcp.json unreadable, projection skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] mcp.json projection failed: %s", exc)


def managed_enabled(st: Dict[str, Any]) -> Dict[str, bool]:
    from features.desktop.capabilities import mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.managed_enabled(account_profile(st))
    except mcp_json.McpJsonError as exc:
        logger.warning("[cloud-bridge] mcp.json managed flags unavailable: %s", exc)
        return {}


def managed_connectors() -> List[Dict[str, Any]]:
    """云端下发的连接器全集（含本机停用的），供本机能力中心展示。

    ``_bridge_context`` 是装配用的，它按本机停用标志把关掉的过滤掉了——界面要的是
    「有哪些」，关掉的也得列出来，开关才有地方可点。启停仍以本机 mcp.json 为准。

    只读已缓存的清单，不触发任何刷新：这是界面读取路径，同步有它自己的时机。
    """
    st = bridge_identity.get_state()
    if not st or not bridge_auth.bridge_enabled():
        return []
    with bridge_client_state.manifest_lock:
        manifest = bridge_client_state.manifest
    if not manifest:
        return []
    enabled = managed_enabled(st)
    out: List[Dict[str, Any]] = []
    for server in manifest.get("servers") or []:
        if not isinstance(server, dict):
            continue
        sid = str(server.get("server_id") or "").strip()
        if not sid:
            continue
        out.append(
            {
                "server_id": sid,
                "display_name": str(server.get("display_name") or sid),
                "description": str(server.get("description") or ""),
                "created_at": server.get("created_at"),
                "enabled": bool(enabled.get(sid, True)),
                "source_plugin": str(server.get("source_plugin") or ""),
                "icon": str(server.get("icon") or ""),
                "tools": [
                    {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                    }
                    for tool in server.get("tools") or []
                    if isinstance(tool, dict)
                ],
            }
        )
    return out


def set_managed_connector_enabled(server_id: str, enabled: bool) -> bool:
    """记下用户对某个云端下发连接器的启停，落本机 mcp.json。

    本机是这个开关的唯一记账处：装配走 ``_bridge_context()``，它就按这里的标志
    过滤，云端不参与也不需要回写。返回 False 表示该 server_id 不是本机托管的
    云端连接器，调用方应继续按普通目录项处理。

    「数据库查询」是个伞形条目（见 ``device_catalog._db_umbrella_item``），本身没有
    对应的 server，开关要落到它收拢的那几个成员上——否则界面上的开关点了没有任何
    东西真的被打开。
    """
    from features.desktop.capabilities import mcp_json
    from features.desktop.capabilities.paths import capabilities_enabled
    from core.capabilities.catalog.loader import DB_HIDDEN_SERVERS, DB_UMBRELLA_ID

    st = bridge_identity.get_state()
    if not capabilities_enabled() or not bridge_auth.bridge_enabled() or not st:
        return False
    profile = account_profile(st)
    if not profile:
        return False
    sid = str(server_id)
    if sid == DB_UMBRELLA_ID:
        members = [
            str(s["server_id"])
            for s in managed_connectors()
            if str(s.get("server_id")) in DB_HIDDEN_SERVERS
        ]
        if not members:
            return False
        for member in members:
            try:
                mcp_json.set_managed_enabled(profile, member, bool(enabled))
            except mcp_json.McpJsonError:
                return False
        return True
    try:
        mcp_json.set_managed_enabled(profile, sid, bool(enabled))
    except mcp_json.McpJsonError:
        return False
    return True

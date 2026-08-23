"""桌面双端「云端能力桥」客户端（本机侧）。

双端模式的本机后端通过本模块把**云端授权的 MCP 工具**合并进本机 Agent 装配：

  桌面壳登录云端 → 换取 capability token → 推送 {cloud_base, token} 到本机
  （POST /v1/desktop/capability/cloud-bridge，CONFIG_TOKEN=桥接秘密）
  → 本模块后台拉取云端 manifest（当前用户最终可用的 MCP 清单）
  → catalog_resolver 解析 enabled_mcp_ids 时把云端 server 追加进清单并按
    组件基名抑制本机重复实现（logical 去重，云端为真源），agent 装配时
    云端 server 以「HTTP MCP 指向云端能力网关」的连接配置接入。

硬边界（与设计文档一致）：
- 本机拿不到云端真实 MCP URL / 密钥——只有网关地址 + capability token；
- 云端断线时云端工具结构化不可用（既有 HTTP MCP 探活/冷却机制兜底），
  **不**静默回退本机同名旧实现；
- ``DESKTOP_LOCAL_MCP_KEEP`` 声明保留在本机的组件基名（默认
  batch_runner / site_publish / generate_chart_tool / automation_task ——
  会话状态、站点、Artifact、定时任务在 P4/P5/P7 桥建成前留本机），
  ``DESKTOP_CLOUD_MCP_BRIDGE_ENABLED=0`` 一键回滚整个桥。

纯本机模式（未配桥）与云端部署（无桥接秘密）零行为变化。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

from core.auth.desktop_bridge import bridge_enabled

logger = logging.getLogger(__name__)

BRIDGE_BLOCK_ID = "desktop_cloud_bridge"

# 双端本机默认保留的组件基名（其余同基名能力以云端为准）。
DEFAULT_LOCAL_KEEP = "batch_runner,site_publish,generate_chart_tool,automation_task"

_MANIFEST_TTL_S = 300.0
_MANIFEST_NEG_TTL_S = 30.0

_state_lock = threading.Lock()
_state: Optional[Dict[str, Any]] = None  # {"cloud_base", "token", "expires_at"}
_state_loaded = False

_manifest_lock = threading.Lock()
_manifest: Optional[Dict[str, Any]] = None
_manifest_ts: float = 0.0
_manifest_error: Optional[str] = None
_manifest_fetching = False


def _bridge_switch_on() -> bool:
    return (os.getenv("DESKTOP_CLOUD_MCP_BRIDGE_ENABLED") or "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def keep_local_bases() -> Set[str]:
    raw = os.getenv("DESKTOP_LOCAL_MCP_KEEP")
    if raw is None or not raw.strip():
        raw = DEFAULT_LOCAL_KEEP
    return {x.strip() for x in raw.split(",") if x.strip()}


# ── 桥状态（内存 + ContentBlock 持久化，重启后无需等壳重推） ────────────


def _load_state_from_db() -> Optional[Dict[str, Any]]:
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, BRIDGE_BLOCK_ID)
            payload = row.payload if row is not None and isinstance(row.payload, dict) else None
            if payload and payload.get("cloud_base") and payload.get("token"):
                return dict(payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cloud-bridge] load state failed: %s", exc)
    return None


def get_state() -> Optional[Dict[str, Any]]:
    global _state, _state_loaded
    with _state_lock:
        if not _state_loaded:
            _state = _load_state_from_db()
            _state_loaded = True
        st = _state
    if not st:
        return None
    if float(st.get("expires_at") or 0) < time.time():
        return None
    return dict(st)


def set_state(cloud_base: str, token: str, expires_in: int) -> None:
    """壳侧推送桥配置（幂等）。立即触发一次后台 manifest 刷新。"""
    global _state, _state_loaded
    payload = {
        "cloud_base": cloud_base.strip().rstrip("/"),
        "token": token.strip(),
        "expires_at": time.time() + max(60, int(expires_in or 0)),
    }
    with _state_lock:
        _state = payload
        _state_loaded = True
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, BRIDGE_BLOCK_ID)
            if row is None:
                db.add(ContentBlock(id=BRIDGE_BLOCK_ID, payload=payload))
            else:
                row.payload = payload
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] persist state failed: %s", exc)
    _refresh_manifest_async(force=True)


def bridge_active() -> bool:
    """本进程是否应启用云端能力桥（仅桌面壳孵化的双端本机后端为 True）。"""
    return _bridge_switch_on() and bridge_enabled() and get_state() is not None


# ── manifest 拉取（后台线程，不阻塞事件循环） ──────────────────────────


def _fetch_manifest_blocking(st: Dict[str, Any]) -> None:
    global _manifest, _manifest_ts, _manifest_error, _manifest_fetching
    url = f"{st['cloud_base']}/api/v1/desktop/capability/manifest"
    try:
        import httpx

        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {st['token']}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        servers = (data or {}).get("servers")
        if not isinstance(servers, list):
            raise ValueError("manifest 结构异常（缺 servers）")
        with _manifest_lock:
            _manifest = {"servers": servers}
            _manifest_ts = time.monotonic()
            _manifest_error = None
        logger.info("[cloud-bridge] manifest 已刷新：云端可用 MCP %d 个", len(servers))
    except Exception as exc:  # noqa: BLE001
        with _manifest_lock:
            _manifest_ts = time.monotonic()
            _manifest_error = str(exc)
        logger.warning("[cloud-bridge] manifest 拉取失败: %s", exc)
    finally:
        with _manifest_lock:
            _manifest_fetching = False


def _refresh_manifest_async(force: bool = False) -> None:
    global _manifest_fetching
    st = get_state()
    if not st:
        return
    with _manifest_lock:
        if _manifest_fetching:
            return
        age = time.monotonic() - _manifest_ts
        ttl = _MANIFEST_NEG_TTL_S if _manifest is None or _manifest_error else _MANIFEST_TTL_S
        if not force and _manifest_ts and age < ttl:
            return
        _manifest_fetching = True
    threading.Thread(
        target=_fetch_manifest_blocking, args=(st,), name="cloud-bridge-manifest", daemon=True
    ).start()


def get_cached_manifest() -> Optional[Dict[str, Any]]:
    """返回缓存的 manifest（可能为 None），并按 TTL 触发后台刷新。

    不做激活判定——守门统一在公共入口（``_bridge_context``）。
    """
    _refresh_manifest_async()
    with _manifest_lock:
        return dict(_manifest) if _manifest else None


# ── 混合能力解析（云端注入 + 本机抑制） ────────────────────────────────


def _bridge_context() -> Optional[Dict[str, Any]]:
    """唯一守门点：桥激活且 manifest 就绪时返回上下文，否则 None。

    返回 {"state": st, "servers": [已剔除 KEEP 基名、含 component 的云端 server]}。
    """
    if not _bridge_switch_on() or not bridge_enabled():
        return None
    st = get_state()
    if not st:
        return None
    manifest = get_cached_manifest()
    if not manifest:
        return None
    keep = keep_local_bases()
    servers: List[Dict[str, Any]] = []
    for s in manifest.get("servers") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("server_id") or "").strip()
        if not sid:
            continue
        base = str(s.get("component") or sid).strip()
        if base in keep:
            continue
        entry = dict(s)
        entry["server_id"] = sid
        entry["component"] = base
        servers.append(entry)
    if not servers:
        return None
    return {"state": st, "servers": servers}


def cloud_gateway_mcp_configs() -> Dict[str, dict]:
    """云端 server → 本机可直接使用的 HTTP MCP 连接配置（指向云端网关）。"""
    ctx = _bridge_context()
    if not ctx:
        return {}
    st = ctx["state"]
    configs: Dict[str, dict] = {}
    for s in ctx["servers"]:
        sid = s["server_id"]
        configs[sid] = {
            "transport": "streamable_http",
            "url": f"{st['cloud_base']}/api/v1/desktop/capability/gateway/{sid}/mcp",
            "headers": {"Authorization": f"Bearer {st['token']}"},
            # 网关串了一跳，给云端工具稍宽的执行预算
            "execution_timeout": 180,
            "is_stable": False,
        }
    return configs


def _local_server_base_map() -> Dict[str, str]:
    """本机 DB 内全部 MCP 的 server_id → 组件基名。

    复用 ``McpServerConfigService`` 自带的 30s 缓存与失效链路（``_row_to_config``
    携带 source_plugin / owner_user_id 元数据），不另建缓存。
    """
    try:
        from core.services.desktop_capability import component_base_name
        from core.services.mcp_service import McpServerConfigService

        cfgs = McpServerConfigService.get_instance().get_all_servers(enabled_only=False)
        return {
            sid: component_base_name(sid, cfg.get("source_plugin"), cfg.get("owner_user_id"))
            for sid, cfg in cfgs.items()
            if isinstance(cfg, dict)
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cloud-bridge] local base map failed: %s", exc)
        return {}


def apply_to_enabled_mcp_ids(mcp_ids: Optional[List[str]]) -> Optional[List[str]]:
    """把云端能力合并进本轮 enabled_mcp_ids，并抑制被云端接管的本机同名实现。

    - 云端 manifest 尚未就绪 / 桥未激活：原样返回（零行为变化）；
    - 抑制规则：本机 server 的组件基名 ∈ 云端合并集合的基名 → 从本轮剔除
      （云端为真源，避免模型看到两份「互联网搜索」）；
    - 云端 server_id 追加到清单尾部（顺序稳定，LLM 前缀缓存友好）；幂等，
      重复应用不产生重复 id。
    """
    if mcp_ids is None:
        return None
    ctx = _bridge_context()
    if not ctx:
        return mcp_ids
    cloud_bases = {s["component"] for s in ctx["servers"]}
    cloud_ids = [s["server_id"] for s in ctx["servers"]]
    local_bases = _local_server_base_map()
    kept = [
        mid
        for mid in mcp_ids
        if mid in cloud_ids or local_bases.get(mid, mid) not in cloud_bases
    ]
    existing = set(kept)
    kept.extend([cid for cid in cloud_ids if cid not in existing])
    return kept


def bridge_status() -> Dict[str, Any]:
    """诊断视图（/v1/desktop/capability/cloud-bridge/status）。"""
    st = get_state()
    with _manifest_lock:
        servers = (_manifest or {}).get("servers") or []
        err = _manifest_error
    return {
        "switch_on": _bridge_switch_on(),
        "configured": st is not None,
        "cloud_base": (st or {}).get("cloud_base"),
        "active": bridge_active(),
        "keep_local": sorted(keep_local_bases()),
        "cloud_server_count": len(servers),
        "cloud_servers": [
            {
                "server_id": s.get("server_id"),
                "component": s.get("component"),
                "display_name": s.get("display_name"),
                "origin": "cloud",
            }
            for s in servers
            if isinstance(s, dict)
        ],
        "last_error": err,
    }


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    global _state, _state_loaded, _manifest, _manifest_ts, _manifest_error, _manifest_fetching
    with _state_lock:
        _state = None
        _state_loaded = False
    with _manifest_lock:
        _manifest = None
        _manifest_ts = 0.0
        _manifest_error = None
        _manifest_fetching = False

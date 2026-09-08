"""桌面双端「云端能力桥」客户端（本机侧）。

双端模式的本机后端通过本模块把**云端授权的 MCP 工具**合并进本机 Agent 装配：

  桌面壳登录云端 → 换取 capability token → 推送 {cloud_base, token} 到本机
  （POST /v1/desktop/capability/cloud-bridge，CONFIG_TOKEN=桥接秘密）
  → 本模块拉取云端 manifest（当前用户最终可用的 MCP 清单）——只在登录、
    云端能力被改动、以及云端在网关调用里告知能力已变时拉取，没有定时轮询
  → catalog_resolver 解析 enabled_mcp_ids 时把云端 server 追加进清单并按
    组件基名抑制本机重复实现（logical 去重，云端为真源），agent 装配时
    直接使用 manifest 内完整 schema 注册虚拟 MCP 工具；模型真正调用后
    才通过普通 JSON 网关在云端网络内执行真实 MCP。

硬边界（与设计文档一致）：
- 本机拿不到云端真实 MCP URL / 密钥——只有网关地址 + capability token；
- 云端断线不会阻塞 Agent 装配；云端工具真正被调用时会返回明确错误，
  **不**静默回退本机同名旧实现；
- ``DESKTOP_LOCAL_MCP_KEEP`` 声明保留在本机的组件基名（默认
  batch_runner / generate_chart_tool / automation_task ——
  会话状态、Artifact、定时任务留本机；正式站点统一云端托管），
  ``DESKTOP_CLOUD_MCP_BRIDGE_ENABLED=0`` 一键回滚整个桥。

纯本机模式（未配桥）与云端部署（无桥接秘密）零行为变化。
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

from core.auth.desktop_bridge import bridge_enabled

logger = logging.getLogger(__name__)

BRIDGE_BLOCK_ID = "desktop_cloud_bridge"

# 双端本机默认保留的组件基名（其余同基名能力以云端为准）。
DEFAULT_LOCAL_KEEP = "batch_runner,generate_chart_tool,automation_task"

# 只能由云端托管的组件基名：正式站点的存储、版本与动态数据必须只有一个家，
# 本机保留会把同一站点的状态劈到两个后端上。运维配置不得放开这条，写了会被
# 忽略并留下告警。
CLOUD_ONLY_BASES = frozenset({"site_publish"})

# 拿不到清单时的有界重试间隔（唯一的时间参数；正常情况下不会用到）。
_MANIFEST_NEG_TTL_S = 30.0

_state_lock = threading.RLock()
_state: Optional[Dict[str, Any]] = None  # {"cloud_base", "token", "expires_at"}
_state_loaded = False

_manifest_lock = threading.Lock()
_manifest: Optional[Dict[str, Any]] = None
_manifest_ts: float = 0.0
_manifest_error: Optional[str] = None
_manifest_fetching = False


def _state_fingerprint(state: Optional[Dict[str, Any]]) -> str:
    """Bind a manifest snapshot to exactly one cloud/token identity."""
    if not state:
        return ""
    from core.services.desktop_capability_protocol import token_subject, token_claims

    token = str(state.get("token") or "")
    # A rotated token for the same account keeps the snapshot; only a real
    # account switch (or an opaque token) invalidates it.
    claims = token_claims(token)
    payload = f"{state.get('cloud_base') or ''}\0{token_subject(token) or token}\0{claims.get('a', '')}\0{claims.get('h', '')}\0{claims.get('d', state.get('device_id') or '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_current_account(state: Dict[str, Any]) -> None:
    """Reject work captured before logout/account switch, including late HTTP replies."""
    from core.capabilities.errors import CloudUnavailable

    if _state_fingerprint(get_state()) != _state_fingerprint(state):
        raise CloudUnavailable("cloud account changed; retry with the current account")


@contextmanager
def account_scope(state: Dict[str, Any]):
    """Serialize a short publication with identity changes; never hold across network IO."""
    with _state_lock:
        require_current_account(state)
        yield


def ensure_current_authorization(state: Optional[Dict[str, Any]] = None) -> None:
    """账号仍然有效才允许使用私有字节；这条判定不发网络请求。

    本机不做「事前探测」——云端清单只在登录、云端能力被改动（前端写操作后调
    ``POST /v1/desktop/capabilities/sync``）以及云端在网关调用里明确告知能力已变
    这三种事件下同步，没有任何定时轮询。真正的撤权由云端在网关调用时裁决：返回
    401/403 会立刻清掉本机的桥状态，被撤权的能力下一次调用即失败。
    """
    from core.capabilities.errors import CloudUnavailable

    st = state or get_state()
    if not st:
        raise CloudUnavailable("cloud authorization expired; sign in again")
    require_current_account(st)


def notify_cloud_changed() -> None:
    """云端能力可能已变（前端写操作 / 网关告知）：后台同步一次清单与本机文件。"""
    _refresh_manifest_async(force=True)


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
    requested = {x.strip() for x in raw.split(",") if x.strip()}
    refused = requested & CLOUD_ONLY_BASES
    if refused:
        logger.warning(
            "[bridge] DESKTOP_LOCAL_MCP_KEEP 忽略只能云端托管的组件基名: %s",
            ", ".join(sorted(refused)),
        )
    return requested - CLOUD_ONLY_BASES


# ── 桥状态（仅内存；壳启动后推送短时运行凭据） ────────────────────


def _purge_persisted_state() -> None:
    """Remove credentials left by an earlier release; bridge tokens are memory-only."""
    try:
        from core.services.desktop_model_credentials import scrub_legacy_rows
        scrub_legacy_rows()
    except Exception as exc:
        logger.warning("[cloud-bridge] legacy model credential cleanup unavailable: %s", type(exc).__name__)
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ContentBlock
        with SessionLocal() as db:
            row = db.get(ContentBlock, BRIDGE_BLOCK_ID)
            if row is not None:
                db.delete(row)
                db.commit()
    except Exception as exc:
        logger.warning("[cloud-bridge] legacy credential cleanup unavailable: %s", type(exc).__name__)


def _load_state_from_db() -> None:
    _purge_persisted_state()
    return None


def cloud_headers(state: Dict[str, Any]) -> Dict[str, str]:
    from core.services.desktop_capability_protocol import token_claims
    device_id = str(state.get("device_id") or token_claims(str(state.get("token") or "")).get("d") or "")
    headers = {"Authorization": f"Bearer {state['token']}"}
    if device_id:
        headers["X-Desktop-Device-Id"] = device_id
    return headers


def clear_state() -> None:
    """Logout invalidates the account immediately, including in-flight responses."""
    global _state, _state_loaded, _manifest, _manifest_error, _manifest_ts
    with _state_lock:
        _state, _state_loaded = None, True
        with _manifest_lock:
            _manifest, _manifest_error, _manifest_ts = None, None, 0.0
        from core.services import desktop_cloud_bundles, desktop_cloud_skills
        desktop_cloud_skills.on_account_switch()
        desktop_cloud_bundles.on_account_switch()
    _purge_persisted_state()
    _rebuild_identity_views()


def _rebuild_identity_views() -> None:
    from core.capabilities import skills
    from core.capabilities.paths import capabilities_enabled
    if not capabilities_enabled():
        return
    from core.agent_skills.cache_refresh import refresh_skill_caches
    refresh_skill_caches()
    shared = skills.device_view_dir()
    root = shared.parent / (shared.name + "_u")
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                skills.rebuild_user_view(child.name)


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


def shell_user_center_id(cloud_base: str, user_center_id: str) -> str:
    """壳送来的桥接用户标识：``cloud:<host>:<port>:<云端 user_center_id>``。

    壳按云端地址给用户加命名空间，同一台机器连不同云端时本机身份天然隔离。本机侧
    用完全相同的规则从凭据里的云端身份推出壳会送来的标识，两者必须逐字相同。
    端口缺省与壳一致：https 443、其它 80。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(cloud_base.strip())
    host = parts.hostname or "cloud"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"cloud:{host}:{port}:{user_center_id}"


def get_identity_state() -> Optional[Dict[str, Any]]:
    """Last shell-authenticated identity for offline local resources; never a cloud grant."""
    from core.services.desktop_capability_protocol import token_claims
    with _state_lock:
        st = dict(_state) if _state else None
    if not st:
        return None
    claims = token_claims(str(st.get("token") or ""))
    if not claims.get("c"):
        return None
    return {"cloud_base": st["cloud_base"], "user_center_id": str(claims["c"]),
            "shell_user_center_id": shell_user_center_id(str(st["cloud_base"]), str(claims["c"])),
            "subject": str(claims.get("u") or ""), "device_id": str(claims.get("d") or ""),
            "authorization_epoch": claims.get("a")}


def set_state(cloud_base: str, token: str, expires_in: int, *, device_id: Optional[str] = None, authorization_epoch: Optional[int] = None) -> None:
    """壳侧推送桥配置（幂等）。立即触发一次后台 manifest 刷新。"""
    global _manifest, _manifest_error, _manifest_ts, _state, _state_loaded
    payload = {
        "cloud_base": cloud_base.strip().rstrip("/"),
        "token": token.strip(),
        "expires_at": time.time() + max(1, int(expires_in or 0)),
        "device_id": device_id,
        "authorization_epoch": authorization_epoch,
    }
    from core.services.desktop_capability_protocol import token_claims
    claims = token_claims(token)
    if device_id and claims.get("d") and device_id != claims["d"]:
        raise ValueError("desktop device identity does not match token")
    if claims.get("e"):
        payload["expires_at"] = min(payload["expires_at"], float(claims["e"]))
    with _state_lock:
        changed = _state_fingerprint(_state) != _state_fingerprint(payload)
        _state = payload
        _state_loaded = True
        if changed:
            # Never expose a previous cloud account's tools while the replacement
            # dynamic manifest is still in flight. Skill files stay in the previous
            # account's isolated profile directory; only the runtime view changes.
            with _manifest_lock:
                _manifest = None
                _manifest_ts = 0.0
                _manifest_error = None
            from core.services import desktop_cloud_bundles, desktop_cloud_skills

            desktop_cloud_skills.on_account_switch()
            desktop_cloud_bundles.on_account_switch()
    _purge_persisted_state()
    if changed:
        _rebuild_identity_views()
    _refresh_manifest_async(force=True)


def bridge_active() -> bool:
    """本进程是否应启用云端能力桥（仅桌面壳孵化的双端本机后端为 True）。"""
    return _bridge_switch_on() and bridge_enabled() and get_state() is not None


# ── manifest 拉取（后台线程，不阻塞事件循环） ──────────────────────────


def _fetch_manifest_blocking(st: Dict[str, Any]) -> None:
    global _manifest, _manifest_ts, _manifest_error, _manifest_fetching
    url = f"{st['cloud_base']}/api/v1/desktop/capability/manifest"
    refresh_replacement = False
    try:
        import httpx
        from core.services.desktop_capability_protocol import validate_manifest

        headers = cloud_headers(st)
        with _manifest_lock:
            current_revision = str((_manifest or {}).get("revision") or "")
        if current_revision:
            headers["If-None-Match"] = f'"{current_revision}"'

        resp = httpx.get(
            url,
            headers=headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        require_current_account(st)
        if resp.status_code in (401, 403):
            with account_scope(st):
                clear_state()
            return
        if resp.status_code == 304:
            with account_scope(st), _manifest_lock:
                _manifest_ts = time.monotonic()
                _manifest_error = None
        else:
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") if isinstance(body, dict) else None
            manifest = validate_manifest(data)
            # A login/account switch may have happened while this request was in
            # flight. Discard the old response instead of racing it into the new
            # account's cache.
            if _state_fingerprint(get_state()) != _state_fingerprint(st):
                logger.info("[cloud-bridge] manifest response discarded after identity change")
                refresh_replacement = True
                return
            with account_scope(st):
                with _manifest_lock:
                    _manifest = manifest
                    _manifest_ts = time.monotonic()
                    _manifest_error = None
                _project_managed_profile(st, manifest)
            logger.info(
                "[cloud-bridge] manifest refreshed revision=%s servers=%d",
                manifest["revision"][:12],
                len(manifest["servers"]),
            )
        # Skills, agents and plugins ride the same poll and token: their intent
        # is reconciled right after the tool manifest is known to be current.
        sync_capabilities_blocking(st)
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            if _state_fingerprint(get_state()) != _state_fingerprint(st):
                refresh_replacement = True
                return
            with _manifest_lock:
                _manifest_ts = time.monotonic()
                _manifest_error = str(exc)
        logger.warning("[cloud-bridge] manifest 拉取失败: %s", exc)
    finally:
        with _manifest_lock:
            _manifest_fetching = False
        if refresh_replacement:
            _refresh_manifest_async(force=True)


def sync_capabilities_blocking(st: Dict[str, Any]) -> None:
    """One pass over every cloud-synced kind (skills, agents, plugins). Blocking; call off-loop.

    Both the background manifest poll and the user-triggered sync endpoint go
    through here so no kind can be forgotten by one of the two paths.
    """
    from core.services import desktop_cloud_bundles, desktop_cloud_skills

    desktop_cloud_skills.sync_blocking(st)
    desktop_cloud_bundles.sync_blocking(st)


def _refresh_manifest_async(force: bool = False) -> None:
    """后台同步一次云端清单。没有定时器：``force`` 由登录 / 变更通知触发，非
    ``force`` 只是「还没有可用清单」时的有界重试（离线登录后的自愈），一旦拿到
    清单就不再有任何后台请求。"""
    global _manifest_fetching
    st = get_state()
    if not st:
        return
    with _manifest_lock:
        if _manifest_fetching:
            return
        if not force:
            if _manifest is not None and not _manifest_error:
                return
            if _manifest_ts and time.monotonic() - _manifest_ts < _MANIFEST_NEG_TTL_S:
                return
        _manifest_fetching = True
    threading.Thread(
        target=_fetch_manifest_blocking, args=(st,), name="cloud-bridge-manifest", daemon=True
    ).start()


def get_cached_manifest() -> Optional[Dict[str, Any]]:
    """返回缓存的 manifest（可能为 None）。

    还没有清单时（离线登录等）触发一次有界重试；已有清单则不发任何请求，更新
    靠登录与变更通知。不做激活判定——守门统一在公共入口（``_bridge_context``）。
    """
    _refresh_manifest_async()
    with _manifest_lock:
        return copy.deepcopy(_manifest) if _manifest else None


# ── 混合能力解析（解析器裁决绑定；mcp.json 投影） ──────────────────────


def _account_profile(st: Dict[str, Any]) -> str:
    """Profile label of the bridged account (opaque tokens get a deterministic label)."""
    from core.capabilities.ref import profile_id
    from core.services.desktop_capability_protocol import token_subject

    return profile_id(str(st["cloud_base"]), token_subject(str(st.get("token") or "")) or "opaque-subject")


def _project_managed_profile(st: Dict[str, Any], manifest: Dict[str, Any]) -> None:
    """Write the account's cloud connector references into mcp.json (managedProfiles)."""
    from core.capabilities import mcp_json
    from core.capabilities.paths import capabilities_enabled
    from core.capabilities.ref import cloud_issuer

    if not capabilities_enabled():
        return
    try:
        mcp_json.project_managed_profile(
            _account_profile(st),
            cloud_instance_id=cloud_issuer(str(st["cloud_base"])),
            catalog_revision=str(manifest["revision"]),
            servers=list(manifest.get("servers") or []),
        )
    except mcp_json.McpJsonCorrupt as exc:
        logger.error("[cloud-bridge] mcp.json unreadable, projection skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-bridge] mcp.json projection failed: %s", exc)


def _managed_enabled(st: Dict[str, Any]) -> Dict[str, bool]:
    from core.capabilities import mcp_json
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.managed_enabled(_account_profile(st))
    except mcp_json.McpJsonError as exc:
        logger.warning("[cloud-bridge] mcp.json managed flags unavailable: %s", exc)
        return {}


def _bridge_context() -> Optional[Dict[str, Any]]:
    """唯一守门点：桥激活且 manifest 就绪时返回上下文，否则 None。

    返回 {"state": st, "servers": [含 component 的云端 server，已剔除 mcp.json 里
    用户停用的项], "profile": 账号 profile}。KEEP 基名与同名裁决交给解析器。
    """
    if not _bridge_switch_on() or not bridge_enabled():
        return None
    st = get_state()
    if not st:
        return None
    manifest = get_cached_manifest()
    if not manifest:
        return None
    enabled = _managed_enabled(st)
    servers: List[Dict[str, Any]] = []
    for s in manifest.get("servers") or []:
        if not isinstance(s, dict):
            continue
        # A server with no current tool contracts cannot replace a local
        # implementation. The manifest validator already guarantees every
        # non-empty entry contains complete, cloud-supplied schemas.
        if not s.get("tools"):
            continue
        sid = str(s.get("server_id") or "").strip()
        if not sid or not enabled.get(sid, True):
            continue
        entry = dict(s)
        entry["server_id"] = sid
        entry["component"] = str(s.get("component") or sid).strip()
        servers.append(entry)
    if not servers:
        return None
    return {
        "state": st,
        "servers": servers,
        "manifest_revision": str(manifest["revision"]),
        "profile": _account_profile(st),
    }


def _mcp_json_local_configs() -> Dict[str, dict]:
    from core.capabilities import mcp_json
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.local_server_configs()
    except mcp_json.McpJsonError as exc:
        logger.warning("[cloud-bridge] mcp.json local servers unavailable: %s", exc)
        return {}


def cloud_gateway_mcp_configs(enabled_mcp_ids: Optional[List[str]] = None, *, resolution_out=None) -> Dict[str, dict]:
    """Desktop-only MCP config sources: cloud gateway bindings + mcp.json local servers.

    Only the components the resolver chose for the cloud binding get a gateway
    config; the run's enabled-id allowlist still collapses the final set.
    """
    from core.capabilities import connectors

    configs: Dict[str, dict] = dict(_mcp_json_local_configs())
    ctx = _bridge_context()
    res = _resolve_mcp_bindings(enabled_mcp_ids, ctx)
    if resolution_out is not None:
        resolution_out.append(res)
    requested = set(enabled_mcp_ids or [])
    for name, group in {**res.conflicts, **res.unusable}.items():
        if name in requested or any(connectors.server_id_of(c) in requested for c in group):
            from core.capabilities.errors import NameConflict, PackageMissing
            error = NameConflict if name in res.conflicts else PackageMissing
            raise error("selected connector binding is unavailable; choose its source", runtime_name=name)
    chosen = res.chosen_ids()
    configs = {
        sid: cfg for sid, cfg in configs.items()
        if f"mcp:{connectors.MCP_JSON_PROFILE}:{sid}" in chosen
    }
    if not ctx:
        return configs
    st = ctx["state"]
    manifest_revision = ctx["manifest_revision"]
    keep = keep_local_bases()
    for s in ctx["servers"]:
        if s["component"] in keep:
            continue  # kept local by deployment policy: no gateway binding is ever built
        sid = s["server_id"]
        if f"mcp:{ctx['profile']}:{sid}" not in chosen:
            continue
        invoke_url = f"{st['cloud_base']}/api/v1/desktop/capability/gateway/{sid}/call"
        configs[sid] = {
            "transport": "streamable_http",
            # HttpMCPConfig requires a URL, but ManifestMCPClient never performs
            # discovery against it. The same JSON endpoint is the only remote hop.
            "url": invoke_url,
            "headers": cloud_headers(st),
            "execution_timeout": 180,
            "is_stable": False,
            "schema_source": "cloud_manifest",
            "manifest_revision": manifest_revision,
            "manifest_tools": copy.deepcopy(s["tools"]),
            "schema_hash": str(s["schema_hash"]),
            "gateway_invoke_url": invoke_url,
            "gateway_component": s["component"],
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
    """把本轮 enabled_mcp_ids 交给解析器，按组件名裁决每个连接器的绑定。

    候选：本机目录行（device）、云端 manifest（account）、mcp.json 本机声明。
    同一组件多个候选时，云端账号级候选默认胜出（``DESKTOP_LOCAL_MCP_KEEP``
    的组件除外），用户可用同名偏好显式改选；落选绑定记为 shadowed，可在界面
    切换，而不是被静默顶掉。云端 manifest 尚未就绪 / 桥未激活：原样返回。
    顺序稳定（本机保留项按原顺序，云端与 mcp.json 项追加），幂等。
    """
    if mcp_ids is None:
        return None
    ctx = _bridge_context()
    from core.capabilities import connectors

    local_bases = _local_server_base_map()
    json_local = _mcp_json_local_declarations()
    res = _resolve_mcp_bindings(mcp_ids, ctx)

    chosen_ids = {connectors.server_id_of(c) for c in res.chosen.values()}
    known = set(local_bases) | {s["server_id"] for s in (ctx or {}).get("servers", [])} | set(json_local)
    kept = [mid for mid in mcp_ids if mid in chosen_ids or mid not in known]
    existing = set(kept)
    for name in sorted(res.chosen):
        sid = connectors.server_id_of(res.chosen[name])
        if sid not in existing:
            kept.append(sid)
            existing.add(sid)
    return kept


def _resolve_mcp_bindings(mcp_ids: Optional[List[str]], ctx):
    from core.capabilities import connectors

    bases = _local_server_base_map()
    enabled = set(bases) if mcp_ids is None else set(mcp_ids)
    candidates = connectors.db_candidates(bases, enabled) + connectors.json_candidates(
        _mcp_json_local_declarations()
    )
    if ctx:
        candidates += connectors.cloud_candidates(ctx["profile"], ctx["servers"], {})
    return connectors.resolve_bindings(candidates, keep_local=keep_local_bases())

def _mcp_json_local_declarations() -> Dict[str, Dict[str, Any]]:
    from core.capabilities import mcp_json
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {}
    try:
        return mcp_json.load().local
    except mcp_json.McpJsonError:
        return {}


def apply_to_enabled_skill_ids(skill_ids: Optional[List[str]]) -> Optional[List[str]]:
    """把云端技能合并进本轮 enabled_skill_ids（云端为真源，见 desktop_cloud_skills）。"""
    if skill_ids is None or not bridge_active():
        return skill_ids
    from core.services import desktop_cloud_skills

    return desktop_cloud_skills.apply_to_enabled_skill_ids(skill_ids)


def bridge_status() -> Dict[str, Any]:
    """诊断视图（/v1/desktop/capability/cloud-bridge/status）。"""
    from core.services import desktop_cloud_skills

    st = get_state()
    with _manifest_lock:
        servers = (_manifest or {}).get("servers") or []
        err = _manifest_error
        manifest_revision = str((_manifest or {}).get("revision") or "")
        schema_ready_count = sum(
            1 for server in servers if isinstance(server, dict) and bool(server.get("tools"))
        )
    return {
        "switch_on": _bridge_switch_on(),
        "configured": st is not None,
        "cloud_base": (st or {}).get("cloud_base"),
        "active": bridge_active(),
        "keep_local": sorted(keep_local_bases()),
        "cloud_server_count": len(servers),
        "manifest_revision": manifest_revision,
        "schema_mode": "dynamic_manifest" if manifest_revision else "none",
        "schema_ready_server_count": schema_ready_count,
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
        "skills": desktop_cloud_skills.status(),
        "bundles": _bundles_status(),
        "mcp_json": _mcp_json_status(),
    }


def _bundles_status() -> Dict[str, Any]:
    from core.services import desktop_cloud_bundles

    return desktop_cloud_bundles.status()


def _mcp_json_status() -> Dict[str, Any]:
    from core.capabilities import connectors, mcp_json
    from core.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return {"enabled": False}
    try:
        doc = mcp_json.load()
    except mcp_json.McpJsonCorrupt as exc:
        return {"enabled": True, "corrupt": True, "error": str(exc)}
    res = connectors.last_resolution()
    return {
        "enabled": True,
        "generation": doc.generation,
        "local_server_count": len(doc.local),
        "managed_profiles": {p: len((v or {}).get("servers") or {}) for p, v in doc.managed.items()},
        "conflicts": sorted(res.conflicts) if res else [],
        "shadowed": sorted(res.shadowed) if res else [],
    }


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    global _state, _state_loaded, _manifest, _manifest_ts, _manifest_error, _manifest_fetching
    from core.services import desktop_cloud_skills

    with _state_lock:
        _state = None
        _state_loaded = False
    with _manifest_lock:
        _manifest = None
        _manifest_ts = 0.0
        _manifest_error = None
        _manifest_fetching = False
    desktop_cloud_skills.reset_for_tests()

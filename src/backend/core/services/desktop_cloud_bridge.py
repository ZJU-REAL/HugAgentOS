"""桌面双端「云端能力桥」客户端（本机侧）。

双端模式的本机后端通过本模块把**云端授权的 MCP 工具**合并进本机 Agent 装配：

  桌面壳登录云端 → 换取 capability token → 推送 {cloud_base, token} 到本机
  （POST /v1/desktop/capability/cloud-bridge，CONFIG_TOKEN=桥接秘密）
  → 本模块拉取云端 manifest（当前用户最终可用的 MCP 清单）——只在登录、
    云端能力被改动、以及云端在网关调用里告知能力已变时拉取，没有定时轮询
  → catalog_resolver 解析 enabled_mcp_ids 时把云端 server 追加进清单并按
    完整 server_id 解析来源（同名候选显式裁决），agent 装配时
    直接使用 manifest 内完整 schema 注册虚拟 MCP 工具；模型真正调用后
    才通过普通 JSON 网关在云端网络内执行真实 MCP。

硬边界（与设计文档一致）：
- 本机拿不到云端真实 MCP URL / 密钥——只有网关地址 + capability token；
- 云端断线不会阻塞 Agent 装配；云端工具真正被调用时会返回明确错误，
  **不**静默回退本机同名旧实现；
- 工具绑定按当前账号授权清单及显式来源选择解析，不使用工具名称保留名单。

纯本机模式（未配桥）与云端部署（无桥接秘密）零行为变化。
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.auth.desktop_bridge import bridge_enabled

logger = logging.getLogger(__name__)

BRIDGE_BLOCK_ID = "desktop_cloud_bridge"

# 拿不到清单时的有界重试间隔（唯一的时间参数；正常情况下不会用到）。
_MANIFEST_NEG_TTL_S = 30.0

_state_lock = threading.RLock()
_state: Optional[Dict[str, Any]] = None  # {"cloud_base", "token", "expires_at"}
_credential_rejected = ""  # account fingerprint whose current token the cloud refused
_state_loaded = False

_manifest_lock = threading.Lock()
_manifest: Optional[Dict[str, Any]] = None
_manifest_ts: float = 0.0
_manifest_error: Optional[str] = None
_manifest_fetching = False
_refresh_pending = False  # a forced refresh arrived while a pass was running


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


# ── 桥状态（仅内存；壳启动后推送短时运行凭据） ────────────────────


def _purge_persisted_state() -> None:
    """Remove credentials left by an earlier release; bridge tokens are memory-only."""
    try:
        from core.services.desktop_model_credentials import scrub_legacy_rows

        scrub_legacy_rows()
    except Exception as exc:
        logger.warning(
            "[cloud-bridge] legacy model credential cleanup unavailable: %s", type(exc).__name__
        )
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, BRIDGE_BLOCK_ID)
            if row is not None:
                db.delete(row)
                db.commit()
    except Exception as exc:
        logger.warning(
            "[cloud-bridge] legacy credential cleanup unavailable: %s", type(exc).__name__
        )


def _load_state_from_db() -> None:
    _purge_persisted_state()
    return None


def current_credentials(state: Dict[str, Any]) -> Dict[str, Any]:
    """The credential belongs to the account, not to the snapshot a pass started with.

    A sync pass can outlive the token it started with while the shell has already
    renewed it. Same account → newest token; another account → untouched, so the
    surrounding identity checks reject it.
    """
    with _state_lock:
        live = _state
    if live and _state_fingerprint(live) == _state_fingerprint(state):
        return live
    return state


def note_rejected_credential(state: Dict[str, Any], exc: BaseException) -> None:
    """A 401 means this account's current token was refused; the next renewal re-syncs."""
    global _credential_rejected
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        with _state_lock:
            _credential_rejected = _state_fingerprint(state)


def cloud_headers(state: Dict[str, Any]) -> Dict[str, str]:
    from core.services.desktop_capability_protocol import token_claims

    state = current_credentials(state)
    device_id = str(
        state.get("device_id") or token_claims(str(state.get("token") or "")).get("d") or ""
    )
    headers = {"Authorization": f"Bearer {state['token']}"}
    if device_id:
        headers["X-Desktop-Device-Id"] = device_id
    return headers


def clear_state() -> None:
    """Logout invalidates the account immediately, including in-flight responses."""
    global _state, _state_loaded, _manifest, _manifest_error, _manifest_ts, _credential_rejected
    with _state_lock:
        _state, _state_loaded = None, True
        _credential_rejected = ""
        _clear_partial_selection()
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
    return {
        "cloud_base": st["cloud_base"],
        "user_center_id": str(claims["c"]),
        "shell_user_center_id": shell_user_center_id(str(st["cloud_base"]), str(claims["c"])),
        "subject": str(claims.get("u") or ""),
        "device_id": str(claims.get("d") or ""),
        "authorization_epoch": claims.get("a"),
    }


def set_state(
    cloud_base: str,
    token: str,
    expires_in: int,
    *,
    device_id: Optional[str] = None,
    authorization_epoch: Optional[int] = None,
) -> None:
    """壳侧推送桥配置（幂等）。立即触发一次后台 manifest 刷新。"""
    global _manifest, _manifest_error, _manifest_ts, _state, _state_loaded, _credential_rejected
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
        renewed_after_rejection = not changed and _credential_rejected == _state_fingerprint(payload)
        _credential_rejected = ""
        _state = payload
        _state_loaded = True
        if changed:
            _clear_partial_selection()
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
    elif renewed_after_rejection:
        _refresh_manifest_async(force=True)


def bridge_active() -> bool:
    """本进程是否应启用云端能力桥（仅桌面壳孵化的双端本机后端为 True）。"""
    return bridge_enabled() and get_state() is not None


# ── manifest 拉取（后台线程，不阻塞事件循环） ──────────────────────────


def _fetch_manifest_blocking(st: Dict[str, Any]) -> None:
    global _manifest, _manifest_ts, _manifest_error, _manifest_fetching, _refresh_pending
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
            rerun = _refresh_pending
            _refresh_pending = False
        if refresh_replacement or rerun:
            _refresh_manifest_async(force=True)


def sync_capabilities_blocking(st: Dict[str, Any]) -> None:
    """One pass over every cloud-synced kind (skills, agents, plugins). Blocking; call off-loop.

    Both the background manifest poll and the user-triggered sync endpoint go
    through here so no kind can be forgotten by one of the two paths.
    """
    from core.services import desktop_cloud_bundles, desktop_cloud_skills

    desktop_cloud_skills.sync_blocking(st)
    desktop_cloud_bundles.sync_blocking(st)
    try:
        from core.capabilities.warmup import warmup_current_account

        warmup_current_account(st)
    except Exception as exc:  # Best effort: ordinary execution retains all checks.
        logger.warning("[caps] startup reads deferred: %s", type(exc).__name__)


def _refresh_manifest_async(force: bool = False) -> None:
    """后台同步一次云端清单。没有定时器：``force`` 由登录 / 变更通知触发，非
    ``force`` 只是「还没有可用清单」时的有界重试（离线登录后的自愈），一旦拿到
    清单就不再有任何后台请求。"""
    global _manifest_fetching, _refresh_pending
    st = get_state()
    if not st:
        return
    with _manifest_lock:
        if _manifest_fetching:
            _refresh_pending = _refresh_pending or force
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
    """返回缓存的 manifest（可能为 None）。调用方只读，不得修改。

    还没有清单时（离线登录等）触发一次有界重试；已有清单则不发任何请求，更新
    靠登录与变更通知。不做激活判定——守门统一在公共入口（``_bridge_context``）。
    清单只在 ``_fetch_manifest_blocking`` 里整体替换，从不原地修改，所以可以安全共享。
    """
    _refresh_manifest_async()
    with _manifest_lock:
        return _manifest


# ── 混合能力解析（解析器裁决绑定；mcp.json 投影） ──────────────────────


def _account_profile(st: Dict[str, Any]) -> str:
    """Profile label of the bridged account (opaque tokens get a deterministic label)."""
    from core.capabilities.ref import profile_id
    from core.services.desktop_capability_protocol import token_subject

    return profile_id(
        str(st["cloud_base"]), token_subject(str(st.get("token") or "")) or "opaque-subject"
    )


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


_context_lock = threading.Lock()
_context_cache: Optional[Tuple[tuple, Dict[str, Any]]] = None


def _bridge_context() -> Optional[Dict[str, Any]]:
    """唯一守门点：桥激活且 manifest 就绪时返回上下文，否则 None。

    返回 {"state": st, "servers": [含 component 的云端 server，已剔除 mcp.json 里
    用户停用的项], "profile": 账号 profile}。同名裁决交给解析器。
    server 列表按 (账号, 清单 revision, 停用集合) 记忆化；``state`` 每次现取，
    因为它携带轮换中的 token。
    """
    global _context_cache
    if not bridge_enabled():
        return None
    st = get_state()
    if not st:
        return None
    manifest = get_cached_manifest()
    if not manifest:
        return None
    enabled = _managed_enabled(st)
    key = (_state_fingerprint(st), str(manifest["revision"]), tuple(sorted(enabled.items())))
    with _context_lock:
        hit = _context_cache
    if hit is None or hit[0] != key:
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
        hit = (
            key,
            {
                "servers": servers,
                "manifest_revision": str(manifest["revision"]),
                "profile": _account_profile(st),
            },
        )
        with _context_lock:
            _context_cache = hit
    if not hit[1]["servers"]:
        return None
    return {"state": st, **hit[1]}


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


def cloud_gateway_mcp_configs(
    enabled_mcp_ids: Optional[List[str]] = None, *, resolution_out=None
) -> Dict[str, dict]:
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
            raise error(
                "selected connector binding is unavailable; choose its source", runtime_name=name
            )
    chosen = res.chosen_ids()
    configs = {
        sid: cfg
        for sid, cfg in configs.items()
        if f"mcp:{connectors.MCP_JSON_PROFILE}:{sid}" in chosen
    }
    if not ctx:
        return configs
    st = ctx["state"]
    manifest_revision = ctx["manifest_revision"]
    for s in ctx["servers"]:
        # Source selection is complete; only the exact authorized cloud binding
        # chosen by the resolver may produce a gateway config.
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
            "manifest_tools": s["tools"],
            "schema_hash": str(s["schema_hash"]),
            "gateway_invoke_url": invoke_url,
            "gateway_plugin": str(s.get("source_plugin") or ""),
        }
    return configs


def _local_server_ids() -> Set[str]:
    """本机 DB 内全部 MCP 的 server_id。

    复用 ``McpServerConfigService`` 自带的 30s 缓存与失效链路，不另建缓存。
    """
    try:
        from core.services.mcp_service import McpServerConfigService

        return set(McpServerConfigService.get_instance().get_all_servers(enabled_only=False))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cloud-bridge] local server ids failed: %s", exc)
        return set()


def apply_to_enabled_mcp_ids(mcp_ids: Optional[List[str]]) -> Optional[List[str]]:
    """把本轮 enabled_mcp_ids 交给解析器，按 server_id 裁决每个连接器的绑定。

    候选：本机目录行（device）、云端 manifest（account）、mcp.json 本机声明。
    同一 server_id 多个候选时，云端账号级候选默认胜出，
    用户可用同名偏好显式改选；落选绑定记为 shadowed，可在界面
    切换，而不是被静默顶掉。云端 manifest 尚未就绪 / 桥未激活：原样返回。
    顺序稳定（已选项按原顺序，新增云端与 mcp.json 项追加），幂等。
    """
    if mcp_ids is None:
        return None
    ctx = _bridge_context()
    from core.capabilities import connectors

    local_ids = _local_server_ids()
    json_local = _mcp_json_local_declarations()
    res = _resolve_mcp_bindings(mcp_ids, ctx)

    chosen_ids = {connectors.server_id_of(c) for c in res.chosen.values()}
    known = local_ids | {s["server_id"] for s in (ctx or {}).get("servers", [])} | set(json_local)
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

    local_ids = _local_server_ids()
    enabled = set(local_ids) if mcp_ids is None else set(mcp_ids)
    candidates = connectors.db_candidates(local_ids, enabled) + connectors.json_candidates(
        _mcp_json_local_declarations()
    )
    if ctx:
        candidates += connectors.cloud_candidates(ctx["profile"], ctx["servers"], {})
    return connectors.resolve_bindings(candidates)


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


def _clear_partial_selection():
    from core.capabilities import session_availability, registry
    if session_availability.active():
        session_availability.clear()
        registry._bump()  # Runtime selection changed even though cloud intent did not.


def retry_initial_sync(st):
    from core.capabilities import skills
    with account_scope(st):
        _clear_partial_selection()
        skills.bump_view_generation()
        _refresh_manifest_async(force=True)


def initial_sync_status() -> Dict[str, Any]:
    """Report actual package counts without disk hashing or network probes."""
    from core.services import desktop_cloud_skills, desktop_cloud_bundles
    from core.capabilities import session_availability
    from core.capabilities.paths import revision_for_hash

    st = get_state()
    if not st:
        return {"ready": False, "syncing": False, "partial": False, "groups": [],
                "pending": 0, "error": "云端身份尚未就绪", "can_continue": False}
    with _manifest_lock:
        manifest_ready = _manifest is not None
        fetching = _manifest_fetching
        error = _manifest_error
    groups = {"skill": desktop_cloud_skills.status(), **desktop_cloud_bundles.status()}
    pending, completed, total = 0, 0, 0
    progress = []
    for kind, group in groups.items():
        error = error or group.get("last_error")
        rows = [row for row in group.get("installations", []) if row.get("state") != "removed"]
        done = sum(bool(row.get("content_hash")) and row.get("state") == "ready"
                   and row.get("resolved_revision") == revision_for_hash(row["content_hash"])
                   for row in rows)
        failed = sum(row.get("state") == "failed" or bool(row.get("last_error")) for row in rows)
        completed += done
        total += len(rows)
        pending += len(rows) - done
        progress.append({"kind": kind, "total": len(rows), "completed": done,
                         "failed": failed, "manifest_ready": bool(group.get("revision"))})
    complete_manifests = all(group.get("revision") for group in groups.values())
    partial = session_availability.active(desktop_cloud_skills._profile(st))
    # Progress is read without the identity lock: package rows may wait on the
    # database, and the shell's token push must never queue behind that wait.
    require_current_account(st)
    complete = (manifest_ready and not fetching and not error and pending == 0 and complete_manifests)
    if not fetching and not complete and not partial:
        error = error or "部分能力未能同步完成，请选择重试或使用已同步能力继续"
    return {"ready": bool(complete or partial), "syncing": fetching, "partial": partial,
            "groups": progress, "completed": completed, "total": total,
            "totals_known": bool(complete_manifests), "pending": pending,
            "error": error, "can_continue": bool(manifest_ready and not fetching and not complete)}


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
        "configured": st is not None,
        "cloud_base": (st or {}).get("cloud_base"),
        "active": bridge_active(),
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
        "managed_profiles": {
            p: len((v or {}).get("servers") or {}) for p, v in doc.managed.items()
        },
        "conflicts": sorted(res.conflicts) if res else [],
        "shadowed": sorted(res.shadowed) if res else [],
    }


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    global _state, _state_loaded, _manifest, _manifest_ts, _manifest_error, _manifest_fetching
    global _credential_rejected, _refresh_pending
    from core.services import desktop_cloud_skills

    with _state_lock:
        _state = None
        _state_loaded = False
        _credential_rejected = ""
    with _manifest_lock:
        _manifest = None
        _manifest_ts = 0.0
        _manifest_error = None
        _manifest_fetching = False
        _refresh_pending = False
    desktop_cloud_skills.reset_for_tests()

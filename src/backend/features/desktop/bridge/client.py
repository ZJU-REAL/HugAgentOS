"""client responsibilities."""
from __future__ import annotations
from features.desktop.capabilities import skills
import threading
import time
from typing import Any, Dict, Optional
import logging
logger = logging.getLogger(__name__)
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.bridge import managed_connectors as bridge_managed_connectors

def notify_cloud_changed() -> None:
    """云端能力可能已变（前端写操作 / 网关告知）：后台同步一次清单与本机文件。"""
    refresh_manifest_async(force=True)


def set_state(
    cloud_base: str,
    token: str,
    expires_in: int,
    *,
    device_id: Optional[str] = None,
    authorization_epoch: Optional[int] = None,
) -> None:
    """壳侧推送桥配置（幂等）。立即触发一次后台 manifest 刷新。"""
    pass  # State is owned by its responsibility module.
    payload = {
        "cloud_base": cloud_base.strip().rstrip("/"),
        "token": token.strip(),
        "expires_at": time.time() + max(1, int(expires_in or 0)),
        "device_id": device_id,
        "authorization_epoch": authorization_epoch,
    }
    from core.capabilities.manifest_protocol import token_claims

    claims = token_claims(token)
    if device_id and claims.get("d") and device_id != claims["d"]:
        raise ValueError("desktop device identity does not match token")
    if claims.get("e"):
        payload["expires_at"] = min(payload["expires_at"], float(claims["e"]))
    with bridge_client_state.state_lock:
        changed = bridge_identity.state_fingerprint(bridge_client_state.state) != bridge_identity.state_fingerprint(payload)
        renewed_after_rejection = not changed and bridge_client_state.credential_rejected == bridge_identity.state_fingerprint(payload)
        bridge_client_state.credential_rejected = ""
        bridge_client_state.state = payload
        bridge_client_state.state_loaded = True
        if changed:
            bridge_identity.clear_partial_selection()
            # Never expose a previous cloud account's tools while the replacement
            # dynamic manifest is still in flight. Skill files stay in the previous
            # account's isolated profile directory; only the runtime view changes.
            with bridge_client_state.manifest_lock:
                bridge_client_state.manifest = None
                bridge_client_state.manifest_ts = 0.0
                bridge_client_state.manifest_error = None
            from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills

            desktop_cloud_skills.on_account_switch()
            desktop_cloud_bundles.on_account_switch()
    bridge_identity.purge_persisted_state()
    if changed:
        bridge_identity.rebuild_identity_views()
        refresh_manifest_async(force=True)
    elif renewed_after_rejection:
        refresh_manifest_async(force=True)


def synced_manifest() -> Optional[Dict[str, Any]]:
    """Read the last accepted connector snapshot without starting a refresh."""
    import copy

    with bridge_client_state.manifest_lock:
        return copy.deepcopy(bridge_client_state.manifest)


def sync_tool_manifest_blocking(st: Dict[str, Any]) -> None:
    """Refresh connectors for both login and explicit synchronization."""
    with bridge_client_state.tool_sync_lock:
        sync_tool_manifest(st)


def sync_tool_manifest(st: Dict[str, Any]) -> None:
    pass  # State is owned by its responsibility module.
    from core.harness.capability_errors import CloudUnavailable

    bridge_identity.require_current_account(st)
    url = f"{st['cloud_base']}/api/v1/desktop/capability/manifest"
    import httpx
    from core.capabilities.manifest_protocol import validate_manifest

    headers = {**bridge_identity.cloud_headers(st), "Cache-Control": "no-cache"}
    with bridge_client_state.manifest_lock:
        current_revision = str((bridge_client_state.manifest or {}).get("revision") or "")
    if current_revision:
        headers["If-None-Match"] = f'"{current_revision}"'

    resp = httpx.get(
        url,
        headers=headers,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    bridge_identity.require_current_account(st)
    if resp.status_code in (401, 403):
        with bridge_identity.account_scope(st):
            bridge_identity.clear_state()
        raise CloudUnavailable("cloud authorization unavailable")
    if resp.status_code == 304:
        with bridge_identity.account_scope(st), bridge_client_state.manifest_lock:
            bridge_client_state.manifest_ts = time.monotonic()
            bridge_client_state.manifest_error = None
    else:
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        manifest = validate_manifest(data)
        with bridge_identity.account_scope(st):
            with bridge_client_state.manifest_lock:
                bridge_client_state.manifest = manifest
                bridge_client_state.manifest_ts = time.monotonic()
                bridge_client_state.manifest_error = None
            bridge_managed_connectors.project_managed_profile(st, manifest)
        logger.info(
            "[cloud-bridge] manifest refreshed revision=%s servers=%d",
            manifest["revision"][:12],
            len(manifest["servers"]),
        )


def fetch_manifest_blocking(st: Dict[str, Any]) -> None:
    pass  # State is owned by its responsibility module.
    refresh_replacement = False
    try:
        sync_tool_manifest_blocking(st)
        # Skills, agents and plugins ride the same poll and token: their intent
        # is reconciled right after the tool manifest is known to be current.
        sync_capabilities_blocking(st)
    except Exception as exc:  # noqa: BLE001
        with bridge_client_state.state_lock:
            if bridge_identity.state_fingerprint(bridge_identity.get_state()) != bridge_identity.state_fingerprint(st):
                refresh_replacement = True
                return
            with bridge_client_state.manifest_lock:
                bridge_client_state.manifest_ts = time.monotonic()
                bridge_client_state.manifest_error = str(exc)
        logger.warning("[cloud-bridge] manifest 拉取失败: %s", exc)
    finally:
        with bridge_client_state.manifest_lock:
            bridge_client_state.manifest_fetching = False
            rerun = bridge_client_state.refresh_pending
            bridge_client_state.refresh_pending = False
        if refresh_replacement or rerun:
            refresh_manifest_async(force=True)


def sync_capabilities_blocking(st: Dict[str, Any]) -> None:
    """One pass over every cloud-synced kind (skills, agents, plugins). Blocking; call off-loop.

    Both the background manifest poll and the user-triggered sync endpoint go
    through here so no kind can be forgotten by one of the two paths.
    """
    from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills

    desktop_cloud_skills.sync_blocking(st)
    desktop_cloud_bundles.sync_blocking(st)
    try:
        from features.desktop.capabilities.warmup import warmup_current_account

        warmup_current_account(st)
    except Exception as exc:  # Best effort: ordinary execution retains all checks.
        logger.warning("[caps] startup reads deferred: %s", type(exc).__name__)


def refresh_manifest_async(force: bool = False) -> None:
    """后台同步一次云端清单。没有定时器：``force`` 由登录 / 变更通知触发，非
    ``force`` 只是「还没有可用清单」时的有界重试（离线登录后的自愈），一旦拿到
    清单就不再有任何后台请求。"""
    pass  # State is owned by its responsibility module.
    st = bridge_identity.get_state()
    if not st:
        return
    with bridge_client_state.manifest_lock:
        if bridge_client_state.manifest_fetching:
            bridge_client_state.refresh_pending = bridge_client_state.refresh_pending or force
            return
        if not force:
            if bridge_client_state.manifest is not None and not bridge_client_state.manifest_error:
                return
            if bridge_client_state.manifest_ts and time.monotonic() - bridge_client_state.manifest_ts < bridge_client_state.MANIFEST_NEG_TTL_S:
                return
        bridge_client_state.manifest_fetching = True
    threading.Thread(
        target=fetch_manifest_blocking, args=(st,), name="cloud-bridge-manifest", daemon=True
    ).start()


def get_cached_manifest() -> Optional[Dict[str, Any]]:
    """返回缓存的 manifest（可能为 None）。调用方只读，不得修改。

    还没有清单时（离线登录等）触发一次有界重试；已有清单则不发任何请求，更新
    靠登录与变更通知。不做激活判定——守门统一在公共入口（``_bridge_context``）。
    清单只在 ``_fetch_manifest_blocking`` 里整体替换，从不原地修改，所以可以安全共享。
    """
    refresh_manifest_async()
    with bridge_client_state.manifest_lock:
        return bridge_client_state.manifest


def retry_initial_sync(st):
    from features.desktop.capabilities import skills
    with bridge_identity.account_scope(st):
        bridge_identity.clear_partial_selection()
        skills.bump_view_generation()
        refresh_manifest_async(force=True)

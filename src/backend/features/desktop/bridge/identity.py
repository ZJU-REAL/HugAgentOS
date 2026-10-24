"""identity responsibilities."""
from __future__ import annotations
from features.desktop.capabilities import skill_paths
from features.desktop.capabilities import skill_views
from contextlib import contextmanager
import hashlib
import time
from typing import Any, Dict, Optional
from features.desktop.auth import bridge as bridge_auth
import logging
logger = logging.getLogger(__name__)
from features.desktop.bridge import client_state as bridge_client_state

def state_fingerprint(state: Optional[Dict[str, Any]]) -> str:
    """Bind a manifest snapshot to exactly one cloud/token identity."""
    if not state:
        return ""
    from core.capabilities.manifest_protocol import token_subject, token_claims

    token = str(state.get("token") or "")
    # A rotated token for the same account keeps the snapshot; only a real
    # account switch (or an opaque token) invalidates it.
    claims = token_claims(token)
    payload = f"{state.get('cloud_base') or ''}\0{token_subject(token) or token}\0{claims.get('a', '')}\0{claims.get('h', '')}\0{claims.get('d', state.get('device_id') or '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_current_account(state: Dict[str, Any]) -> None:
    """Reject work captured before logout/account switch, including late HTTP replies."""
    from core.harness.capability_errors import CloudUnavailable

    if state_fingerprint(get_state()) != state_fingerprint(state):
        raise CloudUnavailable("cloud account changed; retry with the current account")


@contextmanager
def account_scope(state: Dict[str, Any]):
    """Serialize a short publication with identity changes; never hold across network IO."""
    with bridge_client_state.state_lock:
        require_current_account(state)
        yield


def ensure_current_authorization(state: Optional[Dict[str, Any]] = None) -> None:
    """账号仍然有效才允许使用私有字节；这条判定不发网络请求。

    本机不做「事前探测」——云端清单只在登录、云端能力被改动（前端写操作后调
    ``POST /v1/desktop/capabilities/sync``）以及云端在网关调用里明确告知能力已变
    这三种事件下同步，没有任何定时轮询。真正的撤权由云端在网关调用时裁决：返回
    401/403 会立刻清掉本机的桥状态，被撤权的能力下一次调用即失败。
    """
    from core.harness.capability_errors import CloudUnavailable

    st = state or get_state()
    if not st:
        raise CloudUnavailable("cloud authorization expired; sign in again")
    require_current_account(st)


def purge_persisted_state() -> None:
    """Remove credentials left by an earlier release; bridge tokens are memory-only."""
    try:
        from features.desktop.bridge.model_credentials import scrub_legacy_rows

        scrub_legacy_rows()
    except Exception as exc:
        logger.warning(
            "[cloud-bridge] legacy model credential cleanup unavailable: %s", type(exc).__name__
        )
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, bridge_client_state.BRIDGE_BLOCK_ID)
            if row is not None:
                db.delete(row)
                db.commit()
    except Exception as exc:
        logger.warning(
            "[cloud-bridge] legacy credential cleanup unavailable: %s", type(exc).__name__
        )


def load_state_from_db() -> None:
    purge_persisted_state()
    return None


def current_credentials(state: Dict[str, Any]) -> Dict[str, Any]:
    """The credential belongs to the account, not to the snapshot a pass started with.

    A sync pass can outlive the token it started with while the shell has already
    renewed it. Same account → newest token; another account → untouched, so the
    surrounding identity checks reject it.
    """
    with bridge_client_state.state_lock:
        live = bridge_client_state.state
    if live and state_fingerprint(live) == state_fingerprint(state):
        return live
    return state


def note_rejected_credential(state: Dict[str, Any], exc: BaseException) -> None:
    """Only a refusal of the live token schedules re-sync on its next renewal."""
    pass  # State is owned by its responsibility module.
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        authorization = exc.request.headers.get("Authorization")
        with bridge_client_state.state_lock:
            if (
                bridge_client_state.state
                and authorization == f"Bearer {bridge_client_state.state.get('token')}"
                and state_fingerprint(bridge_client_state.state) == state_fingerprint(state)
            ):
                bridge_client_state.credential_rejected = state_fingerprint(state)


def cloud_headers(state: Dict[str, Any]) -> Dict[str, str]:
    from core.capabilities.manifest_protocol import token_claims

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
    pass  # State is owned by its responsibility module.
    with bridge_client_state.state_lock:
        bridge_client_state.state, bridge_client_state.state_loaded = None, True
        bridge_client_state.credential_rejected = ""
        clear_partial_selection()
        with bridge_client_state.manifest_lock:
            bridge_client_state.manifest, bridge_client_state.manifest_error, bridge_client_state.manifest_ts = None, None, 0.0
        from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills

        desktop_cloud_skills.on_account_switch()
        desktop_cloud_bundles.on_account_switch()
    purge_persisted_state()
    rebuild_identity_views()


def rebuild_identity_views() -> None:
    from features.desktop.capabilities import skills
    from features.desktop.capabilities.paths import capabilities_enabled

    if not capabilities_enabled():
        return
    from core.capabilities.skills.cache_refresh import refresh_skill_caches

    refresh_skill_caches()
    shared = skill_paths.device_view_dir()
    root = shared.parent / (shared.name + "_u")
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                skill_views.rebuild_user_view(child.name)


def get_state() -> Optional[Dict[str, Any]]:
    pass  # State is owned by its responsibility module.
    with bridge_client_state.state_lock:
        if not bridge_client_state.state_loaded:
            bridge_client_state.state = load_state_from_db()
            bridge_client_state.state_loaded = True
        st = bridge_client_state.state
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
    from core.capabilities.manifest_protocol import token_claims

    with bridge_client_state.state_lock:
        st = dict(bridge_client_state.state) if bridge_client_state.state else None
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


def bridge_active() -> bool:
    """本进程是否应启用云端能力桥（仅桌面壳孵化的双端本机后端为 True）。"""
    return bridge_auth.bridge_enabled() and get_state() is not None


def clear_partial_selection():
    from features.desktop.capabilities import session_availability, registry
    if session_availability.active():
        session_availability.clear()
        registry._bump()  # Runtime selection changed even though cloud intent did not.


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    pass  # State is owned by its responsibility module.
    pass  # State is owned by its responsibility module.
    from features.desktop.bridge import skills as desktop_cloud_skills

    with bridge_client_state.state_lock:
        bridge_client_state.state = None
        bridge_client_state.state_loaded = False
        bridge_client_state.credential_rejected = ""
    with bridge_client_state.manifest_lock:
        bridge_client_state.manifest = None
        bridge_client_state.manifest_ts = 0.0
        bridge_client_state.manifest_error = None
        bridge_client_state.manifest_fetching = False
        bridge_client_state.refresh_pending = False
    desktop_cloud_skills.reset_for_tests()


def context_headers(*, model=False):
    from features.desktop.observability.context import context_headers as correlate
    return correlate(model=model)


def notify_cloud_changed():
    from features.desktop.bridge.client import notify_cloud_changed as notify
    return notify()

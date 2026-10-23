"""skill account responsibilities."""
from __future__ import annotations
from typing import Optional
from features.desktop.capabilities import registry
from core.capabilities.reference import profile_id
from features.desktop.capabilities import skills_state

def current_account_profile() -> Optional[str]:
    """Profile id of the cloud account this device is currently bridged to."""
    from core.capabilities.manifest_protocol import token_subject
    from features.desktop.bridge.identity import get_state

    st = get_state()
    if not st:
        return None
    subject = token_subject(str(st.get("token") or ""))
    if not subject:
        return None
    return profile_id(str(st["cloud_base"]), subject)


def current_local_user_id() -> Optional[str]:
    """Map the signed stable cloud subject to this device's shadow-user ID."""
    from features.desktop.bridge.identity import get_identity_state

    identity = get_identity_state()
    # 本机影子用户按壳送来的命名空间标识建档（cloud:<host>:<port>:<ucid>），
    # 查找必须用同一标识，而不是凭据里的原始云端 id。
    center = str((identity or {}).get("shell_user_center_id") or "")
    if not center:
        return None
    # user_center_id → user_id is a primary-key mapping fixed at creation; only
    # hits are remembered so a shadow created later is still found.
    hit = skills_state.shadow_user_ids.get(center)
    if hit is not None:
        return hit
    from core.db.models import UserShadow

    with registry._session() as db:
        row = db.query(UserShadow).filter(UserShadow.user_center_id == center).first()
        if row is None:
            return None
        skills_state.shadow_user_ids[center] = str(row.user_id)
        return skills_state.shadow_user_ids[center]


def account_authorized_for(user_id: Optional[str]) -> bool:
    from features.desktop.bridge.identity import get_state

    st = get_state()
    if not st or not str(st.get("token") or "").startswith("dcap2."):
        return False
    current = current_local_user_id()
    return bool(user_id and current and str(user_id) == current)

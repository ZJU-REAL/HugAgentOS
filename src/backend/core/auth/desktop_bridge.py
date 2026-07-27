"""桌面壳 → 本机捆绑后端的身份桥接（混合架构 P2，见 desktop/HYBRID_MODE_DESIGN.md）。

双模式（本机 + 云端）下，桌面端只保留**云端一套用户体系**：用户在云端登录后，
桌面壳（Tauri）把云端身份带给本机后端，本机端不再有独立的账号密码。信任链：

  本机后端由桌面壳孵化，只监听 127.0.0.1，且孵化时注入一次性随机
  ``HUGAGENT_DESKTOP_BRIDGE_SECRET``（存于用户配置目录，不进日志）。
  桌面壳的 Rust 反代对「路由到本机」的请求注入两个头：

    X-Desktop-Bridge:       <secret>            —— 证明请求来自本机壳（恒定时比较）
    X-Desktop-Bridge-User:  base64(JSON)        —— 云端用户信息
                            {"user_center_id": .., "username": .., "email": .., "avatar_url": ..}

  校验通过后按云端 ``user_center_id`` 在本机库 get-or-create shadow 用户——
  与云端同一身份；该用户在本机实例上授予 CE 管理能力（这台机器的主人），
  从而 ``/v1/models/import`` 等 require_config 端点可用于云端配置下发。

云端部署**永不**设置该环境变量 → 本模块在云端是空操作；秘密不匹配/头缺失时
静默返回 None（回落到常规 401 路径），不泄露桥接是否启用。
"""

import base64
import hmac
import json
import os
from typing import Any, Dict, Optional

from core.infra.logging import get_logger
from fastapi import Request
from sqlalchemy.orm import Session

logger = get_logger(__name__)

BRIDGE_SECRET_ENV = "HUGAGENT_DESKTOP_BRIDGE_SECRET"
BRIDGE_SECRET_HEADER = "x-desktop-bridge"
BRIDGE_USER_HEADER = "x-desktop-bridge-user"

# shadow.extra_data 里的幂等标记：首个桥接请求 seed 能力后置位，后续请求零写放行。
_BRIDGE_META_FLAG = "desktop_bridge_admin"


def bridge_enabled() -> bool:
    """本进程是否启用了桌面桥接（仅桌面壳孵化的本机后端为 True）。"""
    return bool(os.getenv(BRIDGE_SECRET_ENV, "").strip())


def _decode_user_header(raw: str) -> Optional[Dict[str, Any]]:
    """解 ``X-Desktop-Bridge-User``（base64 JSON）。畸形输入一律返回 None，不抛异常。"""
    try:
        payload = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_center_id = str(payload.get("user_center_id") or "").strip()
    if not user_center_id:
        return None
    return {
        "user_center_id": user_center_id,
        "username": str(payload.get("username") or "").strip() or user_center_id,
        "email": payload.get("email") or None,
        "avatar_url": payload.get("avatar_url") or None,
    }


def _ensure_bridge_capabilities(db: Session, shadow) -> None:
    """本机实例上授予桥接用户 CE 管理能力（幂等，仅首次写库）。"""
    meta = dict(shadow.extra_data or {})
    if meta.get(_BRIDGE_META_FLAG):
        return
    from core.services.local_user_service import CE_DEFAULT_ADMIN_CAPABILITIES

    meta.update(CE_DEFAULT_ADMIN_CAPABILITIES)
    meta["auth_source"] = "desktop_bridge"
    meta[_BRIDGE_META_FLAG] = True
    # 桥接身份走云端账号，本机不做首启向导/改密要求。
    meta.pop("must_change_password", None)
    meta.pop("onboarding_required", None)
    shadow.extra_data = meta
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(shadow, "extra_data")
    except Exception:  # pragma: no cover - flag_modified 只在个别方言下必要
        pass
    db.commit()


def resolve_bridge_user(request: Request, db: Session):
    """尝试按桌面桥接头解析用户；未启用/未命中/校验失败返回 None（回落常规认证）。

    返回 :class:`core.auth.backend.UserContext`（延迟导入避免环引用）。
    """
    secret = os.getenv(BRIDGE_SECRET_ENV, "").strip()
    if not secret:
        return None
    presented = request.headers.get(BRIDGE_SECRET_HEADER, "")
    if not presented:
        return None
    if not hmac.compare_digest(presented, secret):
        logger.warning("desktop-bridge: secret mismatch (request rejected to normal auth)")
        return None
    info = _decode_user_header(request.headers.get(BRIDGE_USER_HEADER, ""))
    if info is None:
        logger.warning("desktop-bridge: malformed user header")
        return None

    from core.auth.backend import UserContext
    from core.services import UserService

    shadow = UserService(db).get_or_create_user_shadow(
        user_center_id=info["user_center_id"],
        username=info["username"],
        email=info.get("email"),
        avatar_url=info.get("avatar_url"),
    )
    _ensure_bridge_capabilities(db, shadow)
    return UserContext(
        user_id=shadow.user_id,
        user_center_id=shadow.user_center_id,
        username=shadow.username,
        email=shadow.email,
        avatar_url=shadow.avatar_url,
    )

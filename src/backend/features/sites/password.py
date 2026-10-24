"""站点访问密码：哈希存储 + 无状态访问凭据。

密码只以 Argon2id 哈希落库（``sites.access_password_hash``），明文既不存储也不回传。
访问凭据是一枚以该哈希为密钥的 HMAC——不依赖 Redis、不新增表，且改密码即换密钥，
旧凭据自动全部失效。凭据 cookie 的名字、有效期与全部属性都由本模块给全
（``access_cookie_params``），免得"Path 限定 + HttpOnly"这条安全前提散落到路由里走样。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

from core.auth.password import hash_password, verify_password
from core.config.settings import settings
from core.infra.exceptions import BadRequestError

# 访问凭据有效期：过期后访客需重新输入密码。
ACCESS_TTL_SECONDS = 12 * 3600

ACCESS_COOKIE_NAME = "jx_site_access"

# 站点密码是对外分享用的口令，不套用登录账号的强度要求（settings.auth.password_min_length）。
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 128


def site_base_path(slug: str) -> str:
    """站点的托管根路径；cookie 作用域与站内接口地址都从这里推。"""
    return f"/site/{slug}/"


def access_cookie_params(slug: str) -> dict:
    """凭据 cookie 的完整属性。Path 限定到本站点，HttpOnly 挡住站内脚本。"""
    return {
        "key": ACCESS_COOKIE_NAME,
        "max_age": ACCESS_TTL_SECONDS,
        "path": site_base_path(slug),
        "httponly": True,
        "samesite": "lax",
        # 与会话 cookie 共用一个部署开关，免得同一次请求里两枚 cookie 对
        # "本部署是不是 HTTPS" 给出不同答案。
        "secure": settings.session.cookie_secure,
    }


def _normalize_password(raw: Optional[str]) -> str:
    password = (raw or "").strip()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise BadRequestError(f"访问密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise BadRequestError(f"访问密码最多 {MAX_PASSWORD_LENGTH} 位")
    return password


def hash_access_password(raw: str) -> str:
    return hash_password(_normalize_password(raw))


def check_access_password(site, raw: Optional[str]) -> bool:
    return verify_password((raw or "").strip(), site.access_password_hash or "")


def _sign(password_hash: str, site_id: str, expires_at: int) -> str:
    digest = hmac.new(
        password_hash.encode(), f"{site_id}|{expires_at}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_access_token(site) -> str:
    """签发访问凭据；密钥是站点当前的密码哈希，改密码即令旧凭据失效。"""
    expires_at = int(time.time()) + ACCESS_TTL_SECONDS
    return f"{expires_at}.{_sign(site.access_password_hash, site.site_id, expires_at)}"


def verify_access_token(site, token: Optional[str]) -> bool:
    if not token or not site.access_password_hash:
        return False
    raw_expiry, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    if expires_at <= int(time.time()):
        return False
    return hmac.compare_digest(
        signature, _sign(site.access_password_hash, site.site_id, expires_at)
    )

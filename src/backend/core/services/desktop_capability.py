"""桌面双端「云端能力面」服务（云端侧）。

双端模式下，桌面本机后端不再各自维护一套 MCP 能力，而是从云端拉取
「当前用户最终可用」的 MCP 清单（manifest），并把工具调用经云端能力网关
路由回云端真实 MCP 进程。桌面本机侧从 manifest 缓存完整工具 schema，只有
模型真正调用工具时才请求 JSON 调用网关；旧版 MCP 透明反代端点继续兼容。
本模块提供两块地基：

1. **capability token**：短时、最小权限的桌面能力令牌。桌面壳用云端会话
   cookie 换取，再下发给本机后端；本机后端凭它访问 manifest、
   MCP 网关和模型网关。
   ⚠️ 云端 session cookie / 内部 token / 第三方密钥都**不**下发桌面——
   本机只拿到这一枚 HMAC 签名的桌面运行时令牌。
2. **稳定的能力入口**：清单、网关、技能与声明导出按职责放在独立模块；
   此处重导出公开服务函数，路由调用接口保持不变。

设计文档：internal design docs
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from core.services.desktop_capability_credentials import CapabilityContentRejected
from core.services.desktop_capability_entities import (
    build_user_agent_manifest,
    build_user_plugin_manifest,
    resolve_agent_bundle,
    resolve_plugin_bundle,
)

# Public entry points remain stable for route consumers; implementations own their state.
from core.services.desktop_capability_mcp import (
    build_user_capability_manifest,
    invoke_gateway_tool,
    resolve_gateway_target,
    resolve_gateway_tool,
)
from core.services.desktop_capability_models import (
    build_user_model_manifest,
    resolve_model_gateway_target,
)
from core.services.desktop_capability_protocol import (
    CapabilityManifestStaleError,
    build_manifest,
    build_skill_manifest,
    canonical_hash,
    public_tool_schema,
    public_tool_schemas,
    skill_content_hash,
)
from core.services.desktop_capability_security import (
    gateway_stream_secrets,
    guard_capability_bundle,
    guard_capability_content,
    guard_capability_stream,
    invalidate_model_gateway_cache,
)
from core.services.desktop_capability_skills import build_user_skill_manifest, resolve_skill_bundle

logger = logging.getLogger(__name__)

# Access tokens are memory-only credentials; only the shell renews them from
# its still-valid session. Legacy dcap1 tokens are deliberately not accepted.
CAPABILITY_TOKEN_TTL_S = 10 * 60
CAPABILITY_AUDIENCE = "hugagent-desktop-runtime"
CAPABILITY_SCOPE = "desktop_runtime"
CAPABILITY_DEVICE_HEADER = "x-desktop-device-id"

_TOKEN_PREFIX = "dcap2"
_DEVICE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_BLOCK_ID = "desktop_capability_secret"

_secret_cache: Optional[str] = None
_secret_lock = threading.Lock()


# ── 签名密钥（DB 持久化，进程间/重启共享） ──────────────────────────────


def _load_or_create_secret() -> str:
    """get-or-create 服务端签名密钥（content_blocks 单行，随 DB 持久化）。

    有意不从部署级密钥（EMAIL_SECRET_KEY/ADMIN_TOKEN）派生：那些 env 值
    在运维中会被轮换/补配，而桌面令牌的有效性不应随之整体失效。
    """
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    with _secret_lock:
        if _secret_cache:
            return _secret_cache
        with SessionLocal() as db:
            row = db.get(ContentBlock, _SECRET_BLOCK_ID)
            if row is None:
                secret = secrets.token_hex(32)
                row = ContentBlock(id=_SECRET_BLOCK_ID, payload={"secret": secret})
                db.add(row)
                try:
                    db.commit()
                except Exception:
                    # 多 worker 并发首建：让出给先写成功的一方，重读即可。
                    db.rollback()
                    row = db.get(ContentBlock, _SECRET_BLOCK_ID)
            payload = row.payload if isinstance(row.payload, dict) else {}
            secret = str(payload.get("secret") or "").strip()
            if not secret:
                secret = secrets.token_hex(32)
                row.payload = {"secret": secret}
                db.commit()
            _secret_cache = secret
            return secret


def _sign(data: bytes) -> str:
    key = _load_or_create_secret().encode("utf-8")
    return hmac.new(key, data, hashlib.sha256).hexdigest()


# ── token 签发 / 校验 ───────────────────────────────────────────────────


def is_desktop_shell_control(authorization: str, origin: Optional[str]) -> bool:
    """Only the shell process secret authorizes local capability management."""
    from core.auth.desktop_bridge import BRIDGE_SECRET_ENV

    secret = os.getenv(BRIDGE_SECRET_ENV, "").strip()
    if not secret or origin is not None:
        return False
    return hmac.compare_digest(
        (authorization or "").encode("utf-8"), f"Bearer {secret}".encode("utf-8")
    )


def capability_issuer(request_base_url: str) -> str:
    """Normalize the configured instance identity, preserving any tenant path.

    Deployments reached through several proxy aliases should set one explicit
    DESKTOP_CAPABILITY_ISSUER. Never consult untrusted forwarded-host headers.
    """
    raw = (os.getenv("DESKTOP_CAPABILITY_ISSUER") or request_base_url).strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid desktop capability issuer")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid desktop capability issuer")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    scheme = parsed.scheme.lower()
    if port and port != {"http": 80, "https": 443}[scheme]:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))


def session_authorization_epoch(session_data: Dict[str, Any]) -> int:
    """Use the existing session's immutable creation epoch, without a new DB."""
    try:
        created = datetime.fromisoformat(str(session_data.get("created_at") or ""))
        if created.tzinfo is None:
            return 0
        delta = created.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return max(0, (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    except (TypeError, ValueError, OverflowError):
        return 0


def issue_capability_token(
    user_id: str,
    ttl_s: int = CAPABILITY_TOKEN_TTL_S,
    *,
    device_id: str,
    issuer: str,
    session_hash: str,
    authorization_epoch: int,
    user_center_id: str = "",
) -> Dict[str, Any]:
    """Sign a device-bound access token for an already-validated login session."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("invalid capability subject")
    if not isinstance(user_center_id, str) or not user_center_id.strip():
        raise ValueError("invalid capability user center subject")
    if not isinstance(device_id, str) or not _DEVICE_PATTERN.fullmatch(device_id):
        raise ValueError("invalid desktop device id")
    if not isinstance(session_hash, str) or not _HASH_PATTERN.fullmatch(session_hash):
        raise ValueError("invalid capability session")
    if type(authorization_epoch) is not int or authorization_epoch <= 0:
        raise ValueError("invalid authorization epoch")
    ttl = max(60, min(CAPABILITY_TOKEN_TTL_S, int(ttl_s)))
    now = int(time.time())
    payload = json.dumps(
        {
            "u": user_id,
            "c": user_center_id,
            "e": now + ttl,
            "iat": now,
            "n": secrets.token_hex(8),
            "s": CAPABILITY_SCOPE,
            "aud": CAPABILITY_AUDIENCE,
            "iss": capability_issuer(issuer),
            "d": device_id,
            "h": session_hash,
            "a": authorization_epoch,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return {
        "token": f"{_TOKEN_PREFIX}.{body}.{_sign(body.encode('ascii'))}",
        "expires_in": ttl,
        "scope": CAPABILITY_SCOPE,
        "device_id": device_id,
        "authorization_epoch": authorization_epoch,
    }


async def verify_capability_token(
    token: str,
    *,
    device_id: str = "",
    issuer: str = "",
) -> Optional[str]:
    """Validate every claim and the live session; failures never expose details."""
    try:
        if not isinstance(token, str) or len(token) > 4096:
            return None
        if not _DEVICE_PATTERN.fullmatch(device_id):
            return None
        prefix, body, sig = token.strip().split(".", 2)
        if prefix != _TOKEN_PREFIX or not _HASH_PATTERN.fullmatch(sig):
            return None
        if not hmac.compare_digest(sig, _sign(body.encode("ascii"))):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict):
            return None
        now = time.time()
        issued, expires, epoch = payload.get("iat"), payload.get("e"), payload.get("a")
        if any(type(v) is not int for v in (issued, expires, epoch)):
            return None
        if issued <= 0 or issued > now + 30 or expires <= now:
            return None
        if not 0 < expires - issued <= CAPABILITY_TOKEN_TTL_S or epoch <= 0:
            return None
        if (
            payload.get("s") != CAPABILITY_SCOPE
            or payload.get("aud") != CAPABILITY_AUDIENCE
            or payload.get("iss") != capability_issuer(issuer)
            or payload.get("d") != device_id
        ):
            return None
        digest, user_id = payload.get("h"), payload.get("u")
        if not isinstance(digest, str) or not _HASH_PATTERN.fullmatch(digest):
            return None
        if not isinstance(user_id, str) or not user_id.strip():
            return None
        nonce = payload.get("n")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{16}", nonce):
            return None
        from core.auth.session import find_session_by_hash

        current = await find_session_by_hash(digest)
        if not current or str(current.get("user_id") or "") != user_id:
            return None
        center_id = payload.get("c")
        if (
            not isinstance(center_id, str)
            or not center_id.strip()
            or current.get("user_center_id") != center_id
        ):
            return None
        if session_authorization_epoch(current) != epoch:
            return None
        return user_id
    except Exception:
        # Session-store failure is an authorization failure, never an offline
        # bypass. Do not log the token, claims, session digest, or credentials.
        return None

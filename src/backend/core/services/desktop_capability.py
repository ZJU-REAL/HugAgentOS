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
2. **manifest 构建**：复用 catalog resolver + McpServerConfigService 的既有
   授权链路（管理员开关、用户 override、插件安装状态、用户私有 MCP），
   输出 server 级清单（含组件基名，供本机做 logical 去重）。

设计文档：internal design docs
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import copy
import threading
import time
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, unquote, unquote_plus
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.db.engine import SessionLocal
from core.db.models import AdminMcpServer, ContentBlock, ModelProvider, ModelRoleAssignment
from core.services.desktop_capability_protocol import (
    CapabilityManifestStaleError,
    build_manifest,
    build_skill_manifest,
    canonical_hash,
    public_tool_schema,
    public_tool_schemas,
    skill_content_hash,
)

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
    return hmac.compare_digest((authorization or "").encode("utf-8"), f"Bearer {secret}".encode("utf-8"))


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
        {"u": user_id, "c": user_center_id, "e": now + ttl, "iat": now, "n": secrets.token_hex(8),
         "s": CAPABILITY_SCOPE, "aud": CAPABILITY_AUDIENCE,
         "iss": capability_issuer(issuer), "d": device_id,
         "h": session_hash, "a": authorization_epoch},
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return {
        "token": f"{_TOKEN_PREFIX}.{body}.{_sign(body.encode('ascii'))}",
        "expires_in": ttl, "scope": CAPABILITY_SCOPE,
        "device_id": device_id, "authorization_epoch": authorization_epoch,
    }


async def verify_capability_token(
    token: str, *, device_id: str = "", issuer: str = "",
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
        if (payload.get("s") != CAPABILITY_SCOPE or payload.get("aud") != CAPABILITY_AUDIENCE
                or payload.get("iss") != capability_issuer(issuer) or payload.get("d") != device_id):
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
        if not isinstance(center_id, str) or not center_id.strip() or current.get("user_center_id") != center_id:
            return None
        if session_authorization_epoch(current) != epoch:
            return None
        return user_id
    except Exception:
        # Session-store failure is an authorization failure, never an offline
        # bypass. Do not log the token, claims, session digest, or credentials.
        return None


class CapabilityContentRejected(ValueError):
    """A fixed diagnostic; never include the matching credential or content."""

    def __init__(self):
        super().__init__("capability content contains configured credentials or credential policy is unavailable")


# Match credential-bearing field names by whole segment, not substring: a field
# named ``MAX_OUTPUT_TOKENS`` (a numeric limit) must not be treated as a token
# credential just because "TOKENS" contains "token" — otherwise its numeric value
# is scanned as a secret and coincidentally matches bytes in unrelated skill/agent
# bundles, blocking every download with a false "integrity_failed".
_SECRET_FIELD = re.compile(
    r"(?<![A-Za-z0-9])(?:secret|token|password|credential|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|authorization|cookie|key)(?![A-Za-z0-9])",
    re.I,
)
_URL_FIELDS = frozenset({"url", "base_url", "baseurl", "endpoint", "server_url", "api_url", "uri"})


def _secrets_from_config(config: Dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def add(value):
        if isinstance(value, str) and value.strip():
            found.add(value)
            if value.lower().startswith(("bearer ", "basic ")):
                found.add(value.split(" ", 1)[1].strip())
        elif isinstance(value, dict):
            for nested in value.values():
                add(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                add(nested)

    def url_credentials(value):
        if not isinstance(value, str):
            return
        try:
            parsed = urlsplit(value)
            if parsed.password is not None:
                add(parsed.password)
                add(unquote(parsed.password))
                userinfo = parsed.netloc.rsplit("@", 1)[0]
                add(userinfo)
                add(unquote(userinfo))
            elif parsed.username is not None:
                add(parsed.username)
                add(unquote(parsed.username))
            for pair in parsed.query.split("&"):
                raw_key, separator, raw_value = pair.partition("=")
                key = unquote_plus(raw_key)
                if separator and (_SECRET_FIELD.search(key) or key.lower() in ("sig", "signature")):
                    add(raw_value)
                    add(unquote_plus(raw_value))
        except (ValueError, UnicodeError):
            raise CapabilityContentRejected() from None

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_FIELD.search(str(key)):
                    add(item)
                elif str(key).lower() in _URL_FIELDS:
                    url_credentials(item)
                elif isinstance(item, (dict, list, tuple)):
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(config)
    return {value for value in found if value}


def _without_public_identifiers(secrets: set[str], public_identifiers: set[str]) -> set[str]:
    """与模型的公开标识（provider_id / display_name / model_name，模型选择器里所有登录
    用户都看得到）完全相同的值不是机密：把它当机密只会让清单和模型输出因为自己的
    模型名被拒。"""
    return {value for value in secrets if value.strip() not in public_identifiers}


def _model_credentials_and_public_identifiers() -> Tuple[set[str], set[str]]:
    found: set[str] = set()
    public_identifiers: set[str] = set()
    with SessionLocal() as db:
        for provider_id, display_name, model_name, api_key, base_url, extra_config in db.query(
            ModelProvider.provider_id, ModelProvider.display_name, ModelProvider.model_name,
            ModelProvider.api_key, ModelProvider.base_url, ModelProvider.extra_config,
        ).all():
            public_identifiers.update(str(v).strip() for v in (provider_id, display_name, model_name) if v)
            found.update(_secrets_from_config({"api_key": api_key, "base_url": base_url, "extra_config": extra_config}))
    return found, public_identifiers


def _known_cloud_secrets(user_id: str) -> set[str]:
    """Read actual authorized connection credentials into this request only."""
    try:
        keys, configs = _user_effective_configs(user_id, use_cache=False)
        found: set[str] = set()
        for key in keys:
            found.update(_secrets_from_config(configs.get(key) or {}))
        model_secrets, public_identifiers = _model_credentials_and_public_identifiers()
        return _without_public_identifiers(found | model_secrets, public_identifiers)
    except Exception:
        raise CapabilityContentRejected() from None


def gateway_stream_secrets(user_id: str, target: Dict[str, Any]) -> set[str]:
    """网关转发上游模型/MCP 输出时要屏蔽的凭据：已授权连接的凭据 + 本次目标自身的凭据，
    同样排除与模型公开标识相同的值（否则每个流式分片里的 model 字段都会命中）。"""
    try:
        _, public_identifiers = _model_credentials_and_public_identifiers()
        return _without_public_identifiers(
            _known_cloud_secrets(user_id) | _secrets_from_config(target), public_identifiers,
        )
    except CapabilityContentRejected:
        raise
    except Exception:
        raise CapabilityContentRejected() from None


def _secret_bytes(secrets: set[str]) -> set[bytes]:
    values = set()
    for secret in secrets:
        if secret:
            values.add(secret.encode("utf-8"))
            values.add(json.dumps(secret, ensure_ascii=True)[1:-1].encode("ascii"))
    return values


def _guard_value(value: Any, secrets: set[str]) -> None:
    if isinstance(value, str):
        if any(secret in value for secret in secrets):
            raise CapabilityContentRejected()
    elif isinstance(value, bytes):
        if any(secret in value for secret in _secret_bytes(secrets)):
            raise CapabilityContentRejected()
    elif isinstance(value, dict):
        for key, item in value.items():
            _guard_value(key, secrets)
            _guard_value(item, secrets)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _guard_value(item, secrets)


def guard_capability_content(user_id: str, value: Any, *, extra_secrets: Optional[set[str]] = None):
    _guard_value(value, _known_cloud_secrets(user_id) | (extra_secrets or set()))
    return value


def guard_capability_bundle(user_id: str, resolved):
    """Inspect the final ZIP bytes, so a file changed during packing is caught."""
    if resolved is None:
        return None
    import io
    import zipfile

    data, _revision = resolved
    secrets = _known_cloud_secrets(user_id)
    needles = _secret_bytes(secrets)
    keep = max((len(value) for value in needles), default=1) - 1
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            _guard_value(info.filename, secrets)
            with archive.open(info) as file:
                tail = b""
                while chunk := file.read(64 * 1024):
                    combined = tail + chunk
                    if any(value in combined for value in needles):
                        raise CapabilityContentRejected()
                    tail = combined[-keep:] if keep else b""
    return resolved


async def guard_capability_stream(chunks, secrets: set[str]):
    """Keep enough bytes to detect a credential split at any transport boundary."""
    needles = _secret_bytes(secrets)
    keep = max((len(value) for value in needles), default=1) - 1
    tail = b""
    async for chunk in chunks:
        combined = tail + chunk
        if any(value in combined for value in needles):
            raise CapabilityContentRejected()
        count = max(0, len(combined) - keep)
        if count:
            yield combined[:count]
        tail = combined[count:]
    if tail:
        yield tail


# ── 用户有效能力解析（manifest 与网关共用，30s per-user 缓存） ──────────


def component_base_name(
    server_id: str,
    source_plugin: Optional[str],
    owner_user_id: Optional[str] = None,
) -> str:
    """server_id → 组件基名（logical 去重键）。

    两层规范化，与仓库既有 id 机制对齐：
    1. 私有安装的 6 位用户指纹后缀由 ``marketplace_service.base_entry_name``
       剥掉（它就是 catalog 去重用的那套逆函数）；
    2. 插件安装的 ``{slug}-`` 前缀剥掉，得到组件名。
    两端按同一规则计算，云端提供某基名能力时本机抑制同基名旧实现。
    """
    sid = str(server_id or "")
    if owner_user_id:
        try:
            from core.services.marketplace_service import base_entry_name

            sid = base_entry_name(sid, str(owner_user_id))
        except Exception:  # noqa: BLE001 - 指纹剥离失败时退回原 id（仅影响去重精度）
            pass
    slug = str(source_plugin or "").strip()
    if slug and sid.startswith(slug + "-"):
        return sid[len(slug) + 1 :] or sid
    return sid


# 网关每次工具调用都要做归属校验；底层 get_owned_servers 不带缓存（防跨用户
# 泄漏的设计），这里按用户加同节奏的 30s TTL，命中后校验退化为纯内存查找。
_EFFECTIVE_TTL_S = 30.0
_effective_cache: Dict[str, Tuple[float, List[str], Dict[str, dict]]] = {}
_effective_lock = threading.Lock()


def _user_effective_configs(
    user_id: str, *, use_cache: bool = True
) -> Tuple[List[str], Dict[str, dict]]:
    """当前用户最终可用的 (server_id 有序列表, {server_id: 已物化连接配置})。

    复用 agent 装配同一条门控链（catalog resolver + 全局/私有配置合并），
    保证网关授权口径与会话装配完全一致。配置含云端侧凭据，仅进程内使用。
    """
    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _effective_cache.get(uid)
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return list(hit[1]), dict(hit[2])

    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.llm.agent_factory import _effective_mcp_server_keys
    from core.services.mcp_service import McpServerConfigService

    svc = McpServerConfigService.get_instance()
    owned = svc.get_owned_servers(uid)
    with SessionLocal() as db:
        _skills, _agents, mcps = resolve_all_runtime_enabled(db, uid)
    keys = _effective_mcp_server_keys(
        None, None, enabled_mcp_ids=list(mcps or []), owned_servers=owned
    )
    all_cfgs = dict(svc.get_all_servers(enabled_only=True))
    all_cfgs.update(owned)

    with _effective_lock:
        _effective_cache[uid] = (now, list(keys), dict(all_cfgs))
    return keys, all_cfgs


def build_user_capability_manifest(user_id: str) -> Dict[str, Any]:
    """构建当前用户的云端能力 manifest（server 级 + 完整脱敏 schema）。

    只收 ``streamable_http`` 传输的 server——网关按 MCP streamable-http 协议
    透明反代；stdio / sse 传输的（本就极少）不进桌面清单。凭据（URL 内嵌
    密钥、headers、OAuth）一律留在云端连接层，manifest 不携带任何密钥。
    """
    keys, all_cfgs = _user_effective_configs(user_id)

    meta: Dict[str, AdminMcpServer] = {}
    if keys:
        with SessionLocal() as db:
            rows = db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(keys)).all()
            meta = {r.server_id: r for r in rows}
            db.expunge_all()

    servers: List[Dict[str, Any]] = []
    for sid in keys:
        cfg = all_cfgs.get(sid) or {}
        if cfg.get("transport") != "streamable_http":
            continue
        row = meta.get(sid)
        source_plugin = row.source_plugin if row else None
        raw_tools = row.tools_json if row else None
        tools = public_tool_schemas(raw_tools)
        servers.append(
            {
                "server_id": sid,
                "component": component_base_name(
                    sid, source_plugin, row.owner_user_id if row else None
                ),
                "display_name": (row.display_name if row else None) or sid,
                "description": (row.description if row else None) or "",
                "source_plugin": source_plugin,
                "origin": "cloud",
                "execution_scope": "cloud",
                "tools": tools,
                "schema_hash": canonical_hash(tools),
            }
        )
    # The revision intentionally excludes credentials, URLs and timestamps.
    return build_manifest(servers)


def resolve_gateway_target(
    user_id: str, server_id: str, *, fresh: bool = False
) -> Optional[dict]:
    """网关调用前的授权解析：server 必须在该用户当前有效集合内。

    命中返回**已物化**（含云端侧凭据/headers、URL 已去尾斜杠）的连接配置——
    只在云端进程内使用，绝不回传桌面。未命中 / 非 streamable_http / 无 URL
    一律返回 None（调用方 404，不区分“不存在/无权”）。
    """
    keys, all_cfgs = _user_effective_configs(user_id, use_cache=not fresh)
    if server_id not in keys:
        return None
    target = all_cfgs.get(server_id)
    if not isinstance(target, dict) or target.get("transport") != "streamable_http":
        return None
    url = (target.get("url") or "").rstrip("/")
    if not url:
        return None
    target = dict(target)
    target["url"] = url
    return target


def resolve_gateway_tool(
    user_id: str,
    server_id: str,
    tool_name: str,
    *,
    schema_hash: str,
) -> Optional[dict]:
    """Resolve one currently-authorized tool and its private cloud target.

    The desktop's cached schema is discovery data, never an authorization
    grant. Every invocation rechecks both server visibility and the current
    DB tool allowlist before any upstream connection is opened.
    """
    target = resolve_gateway_target(user_id, server_id, fresh=True)
    wanted = str(tool_name or "").strip()
    if target is None or not wanted:
        return None
    with SessionLocal() as db:
        row = db.get(AdminMcpServer, server_id)
        raw_tools = row.tools_json if row is not None else None
    tools = public_tool_schemas(raw_tools)
    if canonical_hash(tools) != str(schema_hash or ""):
        raise CapabilityManifestStaleError("capability manifest changed")
    for tool in tools:
        if tool["name"] == wanted:
            return {
                "user_id": str(user_id),
                "server_id": str(server_id),
                "target": target,
                "tool": tool,
            }
    return None


async def invoke_gateway_tool(
    resolved: Dict[str, Any],
    arguments: Dict[str, Any],
    runtime_headers: Dict[str, str],
) -> Dict[str, Any]:
    """Execute one MCP tool inside the cloud network and return a ToolChunk.

    This deliberately terminates the desktop-facing hop as ordinary JSON. The
    cloud process still uses the native MCP client directly against the private
    target, preserving OAuth, upstream credentials and MCP result conversion
    without extending an MCP SSE session across the public gateway.
    """
    import mcp.types

    from core.llm.mcp_pool import make_client

    target = dict(resolved["target"])
    upstream_headers = {
        str(k).lower(): str(v)
        for k, v in (runtime_headers or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    # Cloud-owned credentials override every desktop-supplied header. Identity
    # is bound to the verified capability token, never to a client header.
    for key, value in dict(target.get("headers") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            upstream_headers[key.lower()] = value
    upstream_headers["x-current-user-id"] = str(resolved["user_id"])
    upstream_headers["accept-encoding"] = "identity"
    target["headers"] = upstream_headers

    client = make_client(str(resolved["server_id"]), target, is_stateful=False)
    raw_tool = mcp.types.Tool.model_validate(resolved["tool"])
    # Skip a second tools/list call in the cloud: the allowlisted schema was read
    # from the same DB row immediately above. get_tool then performs only the
    # real initialize + tools/call lifecycle against the private MCP target.
    client._cached_tools = [raw_tool]  # noqa: SLF001 - AgentScope has no public preload API
    tool = await client.get_tool(raw_tool.name)
    timeout = max(1.0, float(client.execution_timeout or 120.0)) + 10.0
    chunk = await asyncio.wait_for(tool(**dict(arguments or {})), timeout=timeout)
    chunk.metadata.setdefault("origin", "cloud")
    chunk.metadata.setdefault("mcp_server_id", str(resolved["server_id"]))
    return guard_capability_content(str(resolved["user_id"]), chunk.model_dump(mode="json"),
        extra_secrets=_secrets_from_config(target))


# ── 技能清单 / 技能包（云端为真源，本机只缓存文件快照） ───────────────────

_SKILL_SKIP_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}
_skill_manifest_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _skill_snapshot(skill_id: str) -> Optional[Tuple[str, Dict[str, str], Optional[Path]]]:
    """(SKILL.md 正文, {相对路径: 内容}, 文件系统技能目录或 None)。"""
    from core.agent_skills.binary_files import pack_directory
    from core.agent_skills.loader import get_skill_loader

    loader = get_skill_loader()
    info = loader._backend.get_skill_info(skill_id)
    if info is None:
        return None
    if not info.is_database and info.content is None and info.file_path is not None:
        skill_dir = Path(info.file_path).parent
        files = {
            rel: body
            for rel, body in pack_directory(skill_dir).items()
            if not _SKILL_SKIP_PARTS.intersection(rel.split("/")) and not rel.endswith(".pyc")
        }
        return files.pop("SKILL.md", ""), files, skill_dir
    content = loader._backend.read_skill_file(skill_id) if info.is_database else info.content
    return str(content or ""), dict(loader.get_extra_files(skill_id) or {}), None


def build_user_skill_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    """当前用户最终可用技能的清单（含内容哈希）以及云端可见但当前不可用的 id。

    ``skills`` 与会话装配走同一条门控链（catalog resolver + 归属/发布过滤），
    ``suppressed_ids`` 让本机把云端已停用的同名技能一并停掉，做到两端一致。
    """
    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _skill_manifest_cache.get(uid)
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])

    from core.agent_skills.loader import get_skill_loader
    from core.config.catalog_resolver import resolve_all_runtime_enabled
    from core.llm.agent_factory import _filter_skill_ids_for_user

    with SessionLocal() as db:
        enabled, _agents, _mcps = resolve_all_runtime_enabled(db, uid)
    enabled_ids = _filter_skill_ids_for_user(list(enabled or []), uid)
    loader = get_skill_loader()
    metadata = loader.load_all_metadata()
    visible = {sid for sid in metadata if loader.get_skill_owner(sid) in (None, uid)}

    skills: List[Dict[str, Any]] = []
    for sid in enabled_ids:
        meta = metadata.get(sid)
        snapshot = _skill_snapshot(sid) if meta is not None else None
        if snapshot is None:
            continue
        content, files, _dir = snapshot
        skills.append(
            {
                "skill_id": sid,
                "display_name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "scope": "private" if loader.get_skill_owner(sid) else "shared",
                "content_hash": skill_content_hash(content, files),
                "mcp_server_ids": list(meta.mcp_server_ids or []),
            }
        )
    manifest = build_skill_manifest(skills, sorted(visible - {s["skill_id"] for s in skills}))
    with _effective_lock:
        _skill_manifest_cache[uid] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_skill_bundle(user_id: str, skill_id: str) -> Optional[Tuple[bytes, str]]:
    """打包一个当前授权技能为 zip，返回 (bytes, content_hash)；未授权返回 None。"""
    from core.services.marketplace_service import build_skill_zip, build_skill_zip_from_dir

    manifest = build_user_skill_manifest(user_id, use_cache=False)
    if not any(s["skill_id"] == skill_id for s in manifest["skills"]):
        return None
    snapshot = _skill_snapshot(skill_id)
    if snapshot is None:
        return None
    content, files, skill_dir = snapshot
    if skill_dir is not None:
        data = build_skill_zip_from_dir(skill_id, skill_dir)
    else:
        data = build_skill_zip(skill_id, content, files)
    return data, skill_content_hash(content, files)


# ── 智能体 / 插件清单与定义包（云端侧） ──────────────────────────────────
#
# 两类都是纯定义（没有脚本、没有二进制），定义体随清单哈希发布，正文按
# bundle 下发；本机侧按同一哈希核对后落到 R/agents、R/plugins 的 profile 目录。
# 模型服务密钥、企业上游地址永远不进这些文件。

_AGENT_PUBLIC_KEYS = (
    "agent_id",
    "owner_type",
    "name",
    "avatar",
    "description",
    "welcome_message",
    "suggested_questions",
    "mcp_server_ids",
    "skill_ids",
    "plugin_ids",
    "kb_ids",
    "model_provider_id",
    "temperature",
    "max_tokens",
    "max_iters",
    "timeout",
    "is_enabled",
    "sort_order",
    "source_market_slug",
    "ontology_tags",
    "version",
)
_entity_manifest_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


def _zip_files(root: str, files: Dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, body in sorted(files.items()):
            zf.writestr(f"{root}/{rel}", body)
    return buf.getvalue()


_DECLARATION_ENTRY_KEYS = frozenset({
    "kind", "id", "key", "skill_id", "agent_id", "server_id", "required",
    "version_constraint", "version", "platforms", "platform", "execution_plane",
    "architecture", "python_version", "node_version",
})
_RUNTIME_CONSTRAINT_KEYS = ("platforms", "platform", "execution_plane", "architecture", "python_version", "node_version")


def _declaration_atom(value: Any, depth: int = 0) -> Any:
    """Declarations contain scalar metadata, never arbitrary connection objects."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list) and depth < 4:
        return [_declaration_atom(item, depth + 1) for item in value]
    raise CapabilityContentRejected()


def _declaration_entry(value: Any) -> Any:
    if isinstance(value, str):
        return value  # Legacy component ID / runtime package requirement.
    if not isinstance(value, dict):
        raise CapabilityContentRejected()
    return {key: _declaration_atom(item) for key, item in value.items() if key in _DECLARATION_ENTRY_KEYS}


def _declaration_entries(values: Any) -> List[Any]:
    if values is None:
        return []
    return [_declaration_entry(value) for value in (values if isinstance(values, list) else [values])]


def _declaration_groups(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityContentRejected()
    # Unknown groups retain their identities and required flags so the resolver
    # reports unsupported required components instead of making them disappear.
    return {str(group): _declaration_entries(entries) for group, entries in value.items() if group != "warnings"}


def _public_declarations(value: Dict[str, Any]) -> Dict[str, Any]:
    public: Dict[str, Any] = {}
    for key in _RUNTIME_CONSTRAINT_KEYS:
        if key in value:
            public[key] = _declaration_atom(value[key])
    if "dependencies" in value:
        deps = value["dependencies"]
        public["dependencies"] = _declaration_groups(deps) if isinstance(deps, dict) else _declaration_entries(deps)
    if "components" in value:
        public["components"] = _declaration_groups(value["components"])
    if "extensions" in value:
        extensions = value["extensions"]
        if isinstance(extensions, dict):
            public["extensions"] = {
                key: _declaration_atom(item) if key in _RUNTIME_CONSTRAINT_KEYS else (
                    _declaration_entry(item) if isinstance(item, dict) else {"required": True}
                )
                for key, item in extensions.items()
            }
        else:
            public["extensions"] = _declaration_entries(extensions)
    for key in ("hooks", "rules", "commands"):
        if key in value:
            public[key] = _declaration_entries(value[key])
    return public


def _public_agent_extra(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public = {key: _declaration_atom(value[key]) for key in ("version", "ontology_tags") if key in value}
    if "capability_requirements" in value:
        requirements = value["capability_requirements"]
        public["capability_requirements"] = (
            _public_declarations(requirements) if isinstance(requirements, dict) else _declaration_entries(requirements)
        )
    return public


def _agent_files(serialized: Dict[str, Any]) -> Dict[str, str]:
    definition = {k: serialized.get(k) for k in _AGENT_PUBLIC_KEYS}
    definition.update(_public_declarations(serialized))
    definition["extra_config"] = _public_agent_extra(serialized.get("extra_config"))
    return {
        "agent.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2),
        "instructions.md": str(serialized.get("system_prompt") or ""),
    }


def _plugin_files(installed: Dict[str, Any]) -> Dict[str, str]:
    definition = {
        "install_id": installed["install_id"],
        "slug": installed["slug"],
        "name": installed["name"],
        "version": installed.get("version") or "",
        "description": installed.get("description") or "",
        "category": installed.get("category") or "",
        "icon": installed.get("icon"),
        "components": _declaration_groups(installed.get("components") or {
            key: installed.get(key) or [] for key in ("skills", "agents", "mcp", "plugins")
        }),
        "ui_contributions": installed.get("ui_contributions"),
        "import_report": installed.get("import_report") or {},
    }
    definition.update(_public_declarations(installed))
    return {"plugin.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2)}


def _user_agents(user_id: str) -> List[Dict[str, Any]]:
    from core.services.user_agent_service import UserAgentService

    with SessionLocal() as db:
        return list(UserAgentService(db).list_for_user(user_id) or [])


def _user_plugins(user_id: str) -> List[Dict[str, Any]]:
    from core.db.models import InstalledPlugin
    from core.services import plugin_service

    with SessionLocal() as db:
        rows = plugin_service.list_installed(db, user_id, include_global=True)
        metadata = {
            r.install_id: {"ui_contributions": r.ui_contributions, "components": r.component_ids or {}}
            for r in db.query(InstalledPlugin).filter(
                InstalledPlugin.install_id.in_([r["install_id"] for r in rows] or [""])
            )
        }
    for r in rows:
        r.update(metadata.get(r["install_id"], {}))
    return rows


def build_user_agent_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    from core.services.desktop_capability_protocol import build_entity_manifest, entity_content_hash

    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _entity_manifest_cache.get(("agent", uid))
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])
    entries = [
        {
            "agent_id": a["agent_id"],
            "name": a["name"],
            "description": a.get("description") or "",
            "version": str(a.get("version") or ""),
            "content_hash": entity_content_hash(_agent_files(a)),
            "is_enabled": bool(a.get("is_enabled", True)),
        }
        for a in _user_agents(uid)
    ]
    manifest = build_entity_manifest("agent", entries)
    with _effective_lock:
        _entity_manifest_cache[("agent", uid)] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_agent_bundle(user_id: str, agent_id: str) -> Optional[Tuple[bytes, str]]:
    from core.services.desktop_capability_protocol import entity_content_hash

    for a in _user_agents(str(user_id)):
        if a["agent_id"] == agent_id:
            files = _agent_files(a)
            return _zip_files(agent_id, files), entity_content_hash(files)
    return None


def build_user_plugin_manifest(user_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
    from core.services.desktop_capability_protocol import build_entity_manifest, entity_content_hash

    uid = str(user_id)
    now = time.monotonic()
    if use_cache:
        with _effective_lock:
            hit = _entity_manifest_cache.get(("plugin", uid))
            if hit and (now - hit[0]) < _EFFECTIVE_TTL_S:
                return copy.deepcopy(hit[1])
    entries = [
        {
            "install_id": p["install_id"],
            "slug": p["slug"],
            "name": p["name"],
            "version": str(p.get("version") or ""),
            "description": p.get("description") or "",
            "category": p.get("category") or "",
            "content_hash": entity_content_hash(_plugin_files(p)),
            "enabled": bool(p.get("enabled", True)),
            "skills": list(p.get("skills") or []),
            "mcp": list(p.get("mcp") or []),
        }
        for p in _user_plugins(uid)
    ]
    manifest = build_entity_manifest("plugin", entries)
    with _effective_lock:
        _entity_manifest_cache[("plugin", uid)] = (now, copy.deepcopy(manifest))
    return manifest


def resolve_plugin_bundle(user_id: str, install_id: str) -> Optional[Tuple[bytes, str]]:
    from core.services.desktop_capability_protocol import entity_content_hash

    for p in _user_plugins(str(user_id)):
        if p["install_id"] == install_id:
            files = _plugin_files(p)
            return _zip_files(p["slug"], files), entity_content_hash(files)
    return None


# ── 模型清单 / 网关目标（云端真实凭据永不离开本进程） ─────────────────────

_MODEL_PATHS = {
    "chat": "chat/completions",
    "embedding": "embeddings",
    "reranker": "rerank",
}
_SENSITIVE_EXTRA_KEY_PARTS = (
    "api_key",
    "access_key",
    "private_key",
    "secret",
    "password",
    "credential",
    "token",
)


def _model_is_gateway_compatible(provider: ModelProvider) -> bool:
    """桌面模型网关当前承载 OpenAI-compatible 三类协议。

    Azure 会由 SDK 重写 deployment 路径与鉴权头，原生 Anthropic /
    Gemini / Bedrock 也不是同一线上协议；在专用适配器完成前不把它们
    伪装成可用，更不会为了兼容而下发真实凭据。
    """
    from core.llm.providers.registry import get_spec

    provider_id = getattr(provider, "provider", None) or "openai_compatible"
    spec = get_spec(provider_id)
    return spec.engine == "openai" and spec.id != "azure_openai"


def _sanitize_model_extra(value: Any) -> Any:
    """递归剔除 extra_config 里可能的凭据，保留上下文长度等运行参数。"""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if any(part in normalized for part in _SENSITIVE_EXTRA_KEY_PARTS):
                continue
            cleaned[str(key)] = _sanitize_model_extra(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_model_extra(item) for item in value]
    return value


def build_user_model_manifest(user_id: str) -> Dict[str, Any]:
    """返回可安全下发桌面的模型拓扑，不含上游 URL 或任何密钥。

    清单包含全部模型行，使本机旧数据库里曾同步过的明文凭据也会被
    网关占位值覆盖。当前网关不兼容的厂商会下发为 inactive，防止本机
    误调或回落到旧凭据。
    """
    # 用户身份已由 capability token 验证；模型拓扑是全局配置。凭据集合按用户读取，
    # 与出口守卫使用同一来源。
    secrets = _known_cloud_secrets(user_id)
    with SessionLocal() as db:
        providers = db.query(ModelProvider).order_by(ModelProvider.created_at.desc()).all()
        assignments = db.query(ModelRoleAssignment).all()
        rows = []
        withheld = []
        for p in providers:
            row = {
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "provider_type": p.provider_type,
                "provider": getattr(p, "provider", None) or "openai_compatible",
                "model_name": p.model_name,
                "gateway_group": getattr(p, "gateway_group", None),
                "weight": getattr(p, "weight", 1),
                "priority": getattr(p, "priority", 0),
                "extra_config": _sanitize_model_extra(p.extra_config or {}),
                "is_active": bool(p.is_active and _model_is_gateway_compatible(p)),
            }
            collisions = _credential_collisions(row, secrets)
            if collisions:
                # 某个公开字段（如 model_name）与一条已配置的凭据字面相同：下发它就等于
                # 泄漏凭据。只扣留这一条并点名字段，其余模型照常下发；管理员据此改配置。
                withheld.append({"provider_id": p.provider_id, "fields": collisions})
                logger.warning(
                    "[desktop-capability] model provider withheld from manifest: "
                    "provider_id=%s fields=%s collide with a configured credential",
                    p.provider_id, collisions,
                )
                continue
            rows.append(row)
        provider_ids = {row["provider_id"] for row in rows}
        role_rows = [
            {"role_key": a.role_key, "provider_id": a.provider_id}
            for a in assignments
            if a.provider_id in provider_ids
        ]
    manifest = {"version": 1, "providers": rows, "role_assignments": role_rows}
    if withheld:
        manifest["withheld"] = withheld
    return manifest


def _credential_collisions(row: Dict[str, Any], secrets: set[str]) -> List[str]:
    """返回模型行里与已配置凭据字面相撞的字段名（不含值）。"""
    fields: List[str] = []
    for field, value in row.items():
        try:
            _guard_value(value, secrets)
        except CapabilityContentRejected:
            fields.append(field)
    return fields


def _model_provider_allowed(db, user_id: str, provider: ModelProvider) -> bool:  # noqa: ANN001
    """角色模型对所有用户可用；额外对话模型受用户切换能力控制。"""
    assigned = db.query(ModelRoleAssignment).filter(
        ModelRoleAssignment.provider_id == provider.provider_id
    ).first()
    if assigned is not None:
        return True
    if provider.provider_type != "chat":
        return False
    from core.services.user_model_selection import user_can_switch_model

    return user_can_switch_model(db, str(user_id))


def resolve_model_gateway_target(user_id: str, provider_id: str) -> Optional[dict]:
    """解析并授权一个模型上游目标；未授权/不兼容统一返回 None。"""
    pid = str(provider_id or "").strip()
    if not pid:
        return None
    with SessionLocal() as db:
        provider = db.query(ModelProvider).filter(
            ModelProvider.provider_id == pid,
            ModelProvider.is_active == True,  # noqa: E712
        ).first()
        if provider is None or not _model_is_gateway_compatible(provider):
            return None
        path = _MODEL_PATHS.get(str(provider.provider_type or ""))
        base_url = str(provider.base_url or "").strip().rstrip("/")
        if not path or not base_url or not _model_provider_allowed(db, user_id, provider):
            return None
        return {
            "url": f"{base_url}/{path}",
            "api_key": str(provider.api_key or ""),
            "model_name": str(provider.model_name or ""),
            "provider_type": str(provider.provider_type),
            "path": path,
        }

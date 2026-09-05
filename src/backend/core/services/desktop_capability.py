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

# capability token 有效期（秒）。桌面壳每 4 小时刷新一次，24h 给足冗余；
# 令牌只授权 manifest 读取与网关工具调用，泄漏面远小于会话 cookie。
CAPABILITY_TOKEN_TTL_S = 24 * 3600

_TOKEN_PREFIX = "dcap1"
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


def issue_capability_token(user_id: str, ttl_s: int = CAPABILITY_TOKEN_TTL_S) -> Dict[str, Any]:
    """为当前登录用户签发桌面能力令牌。"""
    ttl = max(60, int(ttl_s))
    payload = json.dumps(
        {
            "u": str(user_id),
            "e": int(time.time()) + ttl,
            "n": secrets.token_hex(8),
            "s": "desktop_runtime",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = _sign(body.encode("ascii"))
    return {
        "token": f"{_TOKEN_PREFIX}.{body}.{sig}",
        "expires_in": ttl,
        "scope": "desktop_runtime",
    }


def verify_capability_token(token: str) -> Optional[str]:
    """校验令牌；通过返回 user_id，否则 None（不抛异常、不泄漏失败原因）。"""
    try:
        prefix, body, sig = (token or "").strip().split(".", 2)
        if prefix != _TOKEN_PREFIX:
            return None
        if not hmac.compare_digest(sig, _sign(body.encode("ascii"))):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if int(payload.get("e") or 0) < time.time():
            return None
        user_id = str(payload.get("u") or "").strip()
        return user_id or None
    except Exception:
        return None


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
    return chunk.model_dump(mode="json")


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
    _ = user_id  # 用户身份已由 capability token 验证；模型拓扑是全局配置。
    with SessionLocal() as db:
        providers = db.query(ModelProvider).order_by(ModelProvider.created_at.desc()).all()
        assignments = db.query(ModelRoleAssignment).all()
        provider_ids = {p.provider_id for p in providers}
        rows = [
            {
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
            for p in providers
        ]
        role_rows = [
            {"role_key": a.role_key, "provider_id": a.provider_id}
            for a in assignments
            if a.provider_id in provider_ids
        ]
    return {"version": 1, "providers": rows, "role_assignments": role_rows}


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

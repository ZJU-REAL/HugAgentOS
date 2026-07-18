"""Channel bot service (owner service-account model).

Bind validation → encrypted credential storage → long-connection startup;
runtime CRUD + webhook entry point.
The ``can_create_channel_bot`` capability bit is checked in ``create_bot``.

See internal design docs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.auth.capabilities import resolve_user_capabilities
from core.channels.registry import get_adapter, list_adapters
from core.db.models import ChannelConnection
from core.db.repository.channel import ChannelConnectionRepository
from core.infra.crypto import encrypt_secret
from core.infra.exceptions import (
    AccessDeniedError,
    BadRequestError,
    ResourceNotFoundError,
)

logger = logging.getLogger(__name__)

# Temporary context for WeChat QR-code binding lives in Redis (with TTL, so it
# survives backend restarts + future multi-worker setups; not in process memory):
# key=weixin_bind:{bind_id} → JSON {"owner_id", "qrcode"}, deleted once confirmed.
_WEIXIN_BIND_TTL = 300  # seconds
_WEIXIN_BIND_PREFIX = "weixin_bind:"


def list_owner_conversations(db, owner_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    """List conversations (group/direct) already produced by a user's channel bots, sorted by most recent activity. Used for choosing targets in scheduled delivery.

    Returns ``bot_name`` (bot display name) + ``conversation_id`` (the real Feishu
    conversation ID: group=chat_id / direct=speaker's open_id) + ``chat_type``, so the
    frontend can build distinguishable labels — ``title`` comes from the first message
    content (e.g. "hello"), can collide and is indistinguishable, so it must not be
    used as the sole display name.
    """
    from core.db.models import ChatSession

    rows = (
        db.query(ChatSession)
        .filter(
            ChatSession.user_id == owner_id,
            ChatSession.channel_id.isnot(None),
            ChatSession.deleted_at.is_(None),
        )
        .order_by(ChatSession.last_message_at.desc().nullslast())
        .limit(limit)
        .all()
    )
    # Fetch all relevant bot names in one go (channel_id → display_name), avoiding per-row queries
    chan_ids = {r.channel_id for r in rows if r.channel_id}
    bot_names: Dict[str, str] = {}
    if chan_ids:
        for c in (
            db.query(ChannelConnection)
            .filter(ChannelConnection.channel_id.in_(chan_ids))
            .all()
        ):
            bot_names[c.channel_id] = c.display_name
    return [{
        "channel_id": r.channel_id,
        "bot_name": bot_names.get(r.channel_id),
        "conversation_id": r.external_conversation_id,
        "title": r.title,
        "chat_type": (r.extra_data or {}).get("channel_chat_type"),
        "last_message_at": r.last_message_at.isoformat() if r.last_message_at else None,
    } for r in rows]


def bot_to_dict(conn: ChannelConnection) -> Dict[str, Any]:
    """ORM → safe dict (never echoes encrypted credentials back)."""
    return {
        "channel_id": conn.channel_id,
        "channel_type": conn.channel_type,
        "display_name": conn.display_name,
        "transport": conn.transport,
        "app_id": conn.app_id,
        "status": conn.status,
        "enabled": conn.enabled,
        "agent_id": conn.agent_id,
        "resource_scope": conn.resource_scope,
        "last_event_at": conn.last_event_at.isoformat() if conn.last_event_at else None,
        "last_error": conn.last_error,
        "created_at": conn.created_at.isoformat() if conn.created_at else None,
    }


class ChannelService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = ChannelConnectionRepository(db)

    # ── Queries ─────────────────────────────────────────────────────────
    def list_bots(
        self,
        owner_id: str,
        *,
        agent_id: Optional[str] = None,
        main_only: bool = False,
    ) -> List[ChannelConnection]:
        """``agent_id`` → only bots bound to that subagent; ``main_only`` → only main-agent (unbound) bots."""
        return self.repo.list_by_owner(owner_id, agent_id=agent_id, main_only=main_only)

    def _owned(self, channel_id: str, owner_id: str) -> ChannelConnection:
        conn = self.repo.get_by_id(channel_id)
        if conn is None:
            raise ResourceNotFoundError("channel_bot", channel_id)
        if conn.owner_user_id != owner_id:
            raise AccessDeniedError("无权操作该机器人")
        return conn

    def _validate_agent(self, owner_id: str, agent_id: Optional[str]) -> Optional[str]:
        """Validate agent_id is a subagent the owner can use; non-empty but inaccessible → reject. Returns the normalized id."""
        agent_id = (agent_id or "").strip() or None
        if agent_id is None:
            return None
        from core.services.user_agent_service import UserAgentService

        try:
            UserAgentService(self.db).get_by_id(agent_id, user_id=owner_id)
        except (LookupError, PermissionError):
            raise BadRequestError("无法访问该子智能体")
        return agent_id

    # ── Create / bind ───────────────────────────────────────────────────
    async def create_bot(
        self,
        owner_id: str,
        *,
        channel_type: str,
        app_id: str,
        app_secret: str,
        encrypt_key: Optional[str] = None,
        verification_token: Optional[str] = None,
        extra_credentials: Optional[Dict[str, str]] = None,
        display_name: Optional[str] = None,
        transport: str = "long_conn",
        resource_scope: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> ChannelConnection:
        # 1. Capability bit
        caps = resolve_user_capabilities(self.db, owner_id)
        if not caps.get("can_create_channel_bot"):
            raise AccessDeniedError("无权创建渠道机器人")
        # Binding to a specific subagent (passed when binding from the subagent page): verify ownership. NULL = main agent.
        agent_id = self._validate_agent(owner_id, agent_id)
        # 2. Input params
        if channel_type not in list_adapters():
            raise BadRequestError(f"不支持的渠道类型: {channel_type}")
        if transport not in ("long_conn", "webhook"):
            raise BadRequestError("transport 非法")
        # Only credential-form channels go through create_bot; qr (QR-scan device-flow) channels have a separate bind endpoint
        bind_mode = getattr(get_adapter(channel_type).caps, "bind_mode", "credentials")
        if bind_mode != "credentials":
            raise BadRequestError(f"渠道 {channel_type} 须经扫码绑定，不支持凭据表单")
        app_id = (app_id or "").strip()
        app_secret = (app_secret or "").strip()
        if not app_id or not app_secret:
            raise BadRequestError("App ID / App Secret 不能为空")
        # 3. Token lock: the same app cannot be bound more than once
        if self.repo.get_by_app_id(channel_type, app_id) is not None:
            raise BadRequestError("该应用已被绑定，不能重复绑定")

        # 4. Construct (initially pending), encrypt credentials. app_secret + per-channel extra
        #    credentials (encrypt_key/verification_token/agent_id/token/aes_key…) are uniformly
        #    encrypted and merged into config (key name + _enc suffix).
        channel_id = f"chan_{uuid.uuid4().hex[:16]}"
        config = {"app_secret_enc": encrypt_secret(app_secret)}
        merged_extra: Dict[str, str] = dict(extra_credentials or {})
        if encrypt_key:
            merged_extra.setdefault("encrypt_key", encrypt_key)
        if verification_token:
            merged_extra.setdefault("verification_token", verification_token)
        for key, val in merged_extra.items():
            sval = (val or "").strip()
            if sval:
                config[f"{key}_enc"] = encrypt_secret(sval)

        conn = self.repo.create({
            "channel_id": channel_id,
            "owner_user_id": owner_id,
            "channel_type": channel_type,
            "display_name": (display_name or "我的机器人").strip()[:100],
            "transport": transport,
            "app_id": app_id,
            "config": config,
            "resource_scope": _clean_scope(resource_scope),
            "agent_id": agent_id,
            "status": "pending",
            "enabled": True,
        })

        # 5. Validate credentials (exchange for a token). Failure → mark error but keep the row (user can fix credentials and retry)
        adapter = get_adapter(channel_type)
        try:
            await adapter.validate_credentials(conn)
        except Exception as exc:  # noqa: BLE001
            self.repo.set_status(channel_id, "error", last_error=str(exc)[:480])
            raise BadRequestError(f"凭据校验失败：{exc}")

        # 6. Start the long connection (not needed in webhook mode)
        if transport == "long_conn":
            self._start_long_conn(channel_id, channel_type)
        else:
            self.repo.set_status(channel_id, "connected")

        self.db.refresh(conn)
        return conn

    # ── Update / delete / test ──────────────────────────────────────────
    def update_bot(
        self,
        owner_id: str,
        channel_id: str,
        *,
        display_name: Optional[str] = None,
        enabled: Optional[bool] = None,
        resource_scope: Optional[Dict[str, Any]] = None,
        resource_scope_set: bool = False,
        agent_id: Optional[str] = None,
        agent_id_set: bool = False,
    ) -> ChannelConnection:
        conn = self._owned(channel_id, owner_id)
        patch: Dict[str, Any] = {}
        if display_name is not None:
            patch["display_name"] = display_name.strip()[:100]
        if resource_scope_set:
            patch["resource_scope"] = _clean_scope(resource_scope)
        if agent_id_set:
            # Rebind/unbind the subagent (unbind → fall back to the main agent). Empty string/None both mean unbind.
            patch["agent_id"] = self._validate_agent(owner_id, agent_id)
        if enabled is not None:
            patch["enabled"] = enabled
        if patch:
            self.repo.update(channel_id, patch)
        # An enabled change also toggles the long connection
        if enabled is not None and conn.transport == "long_conn":
            mgr = _manager()
            if enabled:
                mgr.start_connection(channel_id, conn.channel_type)
            else:
                mgr.stop_connection(channel_id)
                self.repo.set_status(channel_id, "disconnected")
        self.db.refresh(conn)
        return conn

    def delete_bot(self, owner_id: str, channel_id: str) -> None:
        conn = self._owned(channel_id, owner_id)
        if conn.transport == "long_conn":
            _manager().stop_connection(channel_id)
        self.repo.delete(channel_id)

    async def test_bot(self, owner_id: str, channel_id: str) -> Dict[str, Any]:
        conn = self._owned(channel_id, owner_id)
        adapter = get_adapter(conn.channel_type)
        try:
            await adapter.validate_credentials(conn)
        except Exception as exc:  # noqa: BLE001
            self.repo.set_status(channel_id, "error", last_error=str(exc)[:480])
            raise BadRequestError(f"凭据校验失败：{exc}")
        # Passing validation counts as healthy: webhook has no persistent connection, and the
        # long connection's credentials are valid at this moment — uniformly set connected
        # (the test is a trusted, user-initiated health signal; don't leave the long
        # connection stuck on stale pending/error)
        self.repo.set_status(channel_id, "connected")
        return {"ok": True}

    # ── WeChat QR-code binding (for qr-mode channels, no credential form) ─
    async def start_weixin_bind(
        self, owner_id: str, *, agent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch the WeChat login QR code, stash the polling context in Redis (TTL), return {bind_id, qrcode_img}.

        Non-empty ``agent_id`` (binding from the subagent page) → verify ownership and carry it
        in the context to the poll phase, where it is written into the connection.
        """
        caps = resolve_user_capabilities(self.db, owner_id)
        if not caps.get("can_create_channel_bot"):
            raise AccessDeniedError("无权创建渠道机器人")
        agent_id = self._validate_agent(owner_id, agent_id)
        adapter = get_adapter("weixin")
        try:
            qr = await adapter.start_qr_bind()
        except Exception as exc:  # noqa: BLE001
            raise BadRequestError(f"获取微信二维码失败：{exc}")
        bind_id = f"wxbind_{uuid.uuid4().hex[:16]}"
        from core.infra.redis import get_redis

        await get_redis().set(
            f"{_WEIXIN_BIND_PREFIX}{bind_id}",
            json.dumps({"owner_id": owner_id, "qrcode": qr["qrcode"], "agent_id": agent_id}),
            ex=_WEIXIN_BIND_TTL,
        )
        return {"bind_id": bind_id, "qrcode_img": qr.get("qrcode_img_content", "")}

    async def poll_weixin_bind(self, owner_id: str, bind_id: str) -> Dict[str, Any]:
        """Poll the QR-scan status; confirmed → persist ChannelConnection + start long polling, returns {status, channel_id?}."""
        from core.infra.redis import get_redis

        redis = get_redis()
        key = f"{_WEIXIN_BIND_PREFIX}{bind_id}"
        raw = await redis.get(key)
        ctx = json.loads(raw) if raw else None
        if ctx is None or ctx.get("owner_id") != owner_id:
            raise ResourceNotFoundError("weixin_bind", bind_id)
        adapter = get_adapter("weixin")
        try:
            status = await adapter.poll_qr_status(ctx["qrcode"])
        except Exception as exc:  # noqa: BLE001
            raise BadRequestError(f"查询扫码状态失败：{exc}")
        if status.get("status") != "confirmed" or not status.get("bot_token"):
            return {"status": status.get("status") or "waiting"}

        # confirmed → create the connection (app_id is derived from bot_token, satisfying the unique constraint)
        await redis.delete(key)
        bot_token = status["bot_token"]
        app_id = f"wx_{hashlib.sha256(bot_token.encode()).hexdigest()[:24]}"
        existing = self.repo.get_by_app_id("weixin", app_id)
        if existing is not None:
            return {"status": "confirmed", "channel_id": existing.channel_id}
        channel_id = f"chan_{uuid.uuid4().hex[:16]}"
        config = {"bot_token_enc": encrypt_secret(bot_token)}
        if status.get("baseurl"):
            config["baseurl"] = status["baseurl"]
        self.repo.create({
            "channel_id": channel_id,
            "owner_user_id": owner_id,
            "channel_type": "weixin",
            "display_name": "我的微信机器人",
            "transport": "long_conn",
            "app_id": app_id,
            "config": config,
            "resource_scope": None,
            "agent_id": ctx.get("agent_id"),
            "status": "connected",
            "enabled": True,
        })
        self._start_long_conn(channel_id, "weixin")
        return {"status": "confirmed", "channel_id": channel_id}

    # ── Webhook entry (for webhook-mode channels) ──────────────────────
    def handle_webhook(
        self, channel_id: str, headers: Dict[str, str], body: bytes
    ) -> Dict[str, Any]:
        """Handle the webhook synchronously: URL verification returns the challenge; message events dispatch the async handle_inbound.

        Returns the response body (dict) sent back to the channel. Signature verification
        failure raises AccessDeniedError.
        """
        import asyncio
        import json

        conn = self.repo.get_by_id(channel_id)
        if conn is None or not conn.enabled:
            raise ResourceNotFoundError("channel_bot", channel_id)
        adapter = get_adapter(conn.channel_type)

        # Decrypt (if encryption is configured). WeCom's decrypt needs headers to backfill
        # `encrypt` for signature verification → takes one extra argument; Feishu's decrypt
        # only takes two args → fall back via TypeError, keeping one entry point for both channels.
        decrypt = getattr(adapter, "decrypt_webhook", None)
        if decrypt is not None:
            try:
                payload = decrypt(conn, body, headers)
            except TypeError:
                payload = decrypt(conn, body)
        else:
            payload = json.loads(body or b"{}")
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        if not adapter.verify_webhook(conn, headers, body):
            raise AccessDeniedError("webhook 验签失败")

        inbound = adapter.parse_inbound(conn, payload)
        if inbound is None:
            return {"code": 0}  # non-message event, ack
        from core.channels.inbound import handle_inbound

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(handle_inbound(inbound))
        except RuntimeError:
            # Not inside an event loop (rare) — synchronous fallback
            asyncio.run(handle_inbound(inbound))
        return {"code": 0}

    def handle_webhook_get(self, channel_id: str, params: Dict[str, str]) -> str:
        """GET URL verification (used when configuring the WeCom callback): verify the signature, then return the plaintext echostr.

        Channels without ``verify_url`` (e.g. Feishu uses the POST challenge) → return
        echostr/empty string as-is.
        """
        conn = self.repo.get_by_id(channel_id)
        if conn is None or not conn.enabled:
            raise ResourceNotFoundError("channel_bot", channel_id)
        adapter = get_adapter(conn.channel_type)
        verify_url = getattr(adapter, "verify_url", None)
        if verify_url is None:
            return params.get("echostr", "")
        try:
            return verify_url(conn, params)
        except Exception as exc:  # noqa: BLE001
            raise AccessDeniedError(f"URL 校验失败: {exc}")

    # ── Internal ────────────────────────────────────────────────────────
    def _start_long_conn(self, channel_id: str, channel_type: str) -> None:
        try:
            _manager().start_connection(channel_id, channel_type)
        except Exception:  # noqa: BLE001
            logger.exception("[channels] 启动长连接失败 %s", channel_id)


def _manager():
    from core.channels.manager import get_manager

    return get_manager()


def _clean_scope(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize the resource whitelist: keep only string lists for kb_ids / skill_ids; empty → None (exposes everything)."""
    if not isinstance(scope, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("kb_ids", "skill_ids"):
        val = scope.get(key)
        if isinstance(val, list) and val:
            out[key] = [str(x) for x in val]
    return out or None

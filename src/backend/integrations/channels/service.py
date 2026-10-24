"""Channel bot service (owner service-account model).

Bind validation → encrypted credential storage → long-connection startup;
runtime CRUD + webhook entry point.
The ``can_create_channel_bot`` capability bit is checked in ``create_bot``.

See internal design docs.
"""

from __future__ import annotations

from core.db.threads import run_in_session
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.auth.capabilities import resolve_user_capabilities
from integrations.channels.registry import get_adapter, list_adapters
from integrations.channels.models import ChannelConnection
from integrations.channels.repository import ChannelConnectionRepository
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
    from integrations.channels.relay import public_execution
    return {
        **public_execution(conn),
        "channel_id": conn.channel_id,
        "channel_type": conn.channel_type,
        "display_name": conn.display_name,
        "transport": conn.transport,
        "app_id": conn.app_id,
        "status": conn.status,
        "enabled": conn.enabled,
        "agent_id": conn.agent_id,
        "group_listen_mode": conn.group_listen_mode,
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
        from core.capabilities.agents.edition import UserAgentService

        try:
            UserAgentService(self.db).get_by_id(agent_id, user_id=owner_id)
        except (LookupError, PermissionError):
            raise BadRequestError("无法访问该子智能体")
        return agent_id

    # ── Create / bind ───────────────────────────────────────────────────
    def register_bot(
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
        group_listen_mode: str = "mention_only",
        local_binding_id: Optional[str] = None,
    ) -> ChannelConnection:
        """Validate and store a pending bot (credentials encrypted); returns it detached."""
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

        if local_binding_id:
            from integrations.channels.relay import ChannelRelayService
            config["desktop_execution"] = ChannelRelayService(self.db).bind(owner_id, local_binding_id)

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
            "group_listen_mode": _validate_group_listen(channel_type, group_listen_mode),
            "status": "pending",
            "enabled": True,
        })

        self.db.expunge(conn)
        return conn

    def mark_connected(self, channel_id: str) -> ChannelConnection:
        self.repo.set_status(channel_id, "connected")
        return self.detached(channel_id)

    def mark_failed(self, channel_id: str, error: Exception) -> None:
        self.repo.set_status(channel_id, "error", last_error=str(error)[:480])

    def detached(self, channel_id: str) -> ChannelConnection:
        conn = self.repo.get_by_id(channel_id)
        self.db.expunge(conn)
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
        group_listen_mode: Optional[str] = None,
    ) -> ChannelConnection:
        conn = self._owned(channel_id, owner_id)
        was_enabled = conn.enabled
        patch: Dict[str, Any] = {}
        if group_listen_mode is not None:
            patch["group_listen_mode"] = _validate_group_listen(
                conn.channel_type, group_listen_mode
            )
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
        if enabled is not None and enabled != was_enabled and conn.transport == "long_conn":
            mgr = _manager()
            if enabled:
                self.repo.set_status(channel_id, "pending")
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

    def owned_detached(self, channel_id: str, owner_id: str) -> ChannelConnection:
        conn = self._owned(channel_id, owner_id)
        self.db.expunge(conn)
        return conn

    # ── WeChat QR-code binding (for qr-mode channels, no credential form) ─
    def admit_bind(self, owner_id: str, agent_id: Optional[str]) -> Optional[str]:
        """Capability check for a new bot; returns the validated sub-agent id."""
        caps = resolve_user_capabilities(self.db, owner_id)
        if not caps.get("can_create_channel_bot"):
            raise AccessDeniedError("无权创建渠道机器人")
        return self._validate_agent(owner_id, agent_id)

    def record_weixin_bind(
        self, owner_id: str, ctx: Dict[str, Any], status: Dict[str, Any]
    ) -> tuple[str, bool]:
        """Store a confirmed WeChat binding; returns ``(channel_id, created)``."""
        bot_token = status["bot_token"]
        app_id = f"wx_{hashlib.sha256(bot_token.encode()).hexdigest()[:24]}"
        existing = self.repo.get_by_app_id("weixin", app_id)
        if existing is not None:
            if existing.owner_user_id != owner_id:
                raise AccessDeniedError("该微信已被其他用户绑定")
            from integrations.channels.relay import execution
            selected_local = bool(ctx.get("local_binding_id"))
            current_binding = execution(existing)
            if selected_local or current_binding:
                raise BadRequestError("该微信已添加，请先移除原机器人，再按所选执行位置重新添加")
            user_id = status.get("ilink_user_id")
            if isinstance(user_id, str) and user_id.strip():
                existing.config = {**(existing.config or {}), "ilink_user_id": user_id}
                self.db.commit()
            return existing.channel_id, False
        channel_id = f"chan_{uuid.uuid4().hex[:16]}"
        config = {"bot_token_enc": encrypt_secret(bot_token)}
        user_id = status.get("ilink_user_id")
        if isinstance(user_id, str) and user_id.strip():
            config["ilink_user_id"] = user_id
        if ctx.get("local_binding_id"):
            from integrations.channels.relay import ChannelRelayService
            config["desktop_execution"] = ChannelRelayService(self.db).bind(owner_id, ctx["local_binding_id"])
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
            "status": "pending",
            "enabled": True,
        })
        return channel_id, True

    # ── Webhook entry (for webhook-mode channels) ──────────────────────
    def handle_webhook(
        self, channel_id: str, headers: Dict[str, str], body: bytes
    ) -> tuple[Dict[str, Any], Any]:
        """Verify and parse a webhook delivery (database + crypto only).

        Returns ``(response body for the channel, inbound message or None)``; the
        caller dispatches the inbound message on the event loop. Signature
        verification failure raises AccessDeniedError.
        """
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
            return {"challenge": payload.get("challenge", "")}, None

        if not adapter.verify_webhook(conn, headers, body):
            raise AccessDeniedError("webhook 验签失败")

        # A non-message event parses to None and is only acknowledged.
        return {"code": 0}, adapter.parse_inbound(conn, payload)

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
def _mark_failed(db: Session, channel_id: str, error: Exception) -> None:
    ChannelService(db).mark_failed(channel_id, error)


async def create_bot(owner_id: str, **fields: Any) -> ChannelConnection:
    """Store a bot, check its credentials with the platform, then start receiving.

    The credential check is a network call between two short database steps; a
    failed check keeps the row (marked ``error``) so the owner can fix it.
    """
    conn = await run_in_session(lambda db: ChannelService(db).register_bot(owner_id, **fields))
    try:
        await get_adapter(conn.channel_type).validate_credentials(conn)
    except Exception as exc:  # noqa: BLE001
        await run_in_session(_mark_failed, conn.channel_id, exc)
        raise BadRequestError(f"凭据校验失败：{exc}")
    if conn.transport == "long_conn":
        _start_long_conn(conn.channel_id, conn.channel_type)
        return await run_in_session(lambda db: ChannelService(db).detached(conn.channel_id))
    return await run_in_session(lambda db: ChannelService(db).mark_connected(conn.channel_id))


async def test_bot(owner_id: str, channel_id: str) -> Dict[str, Any]:
    conn = await run_in_session(lambda db: ChannelService(db).owned_detached(channel_id, owner_id))
    try:
        await get_adapter(conn.channel_type).validate_credentials(conn)
    except Exception as exc:  # noqa: BLE001
        await run_in_session(_mark_failed, channel_id, exc)
        raise BadRequestError(f"凭据校验失败：{exc}")
    # Credentials and receiver health are separate. Only the receiver can prove
    # that a long connection is running; an HTTP worker may not own that receiver.
    if conn.enabled and conn.transport == "webhook":
        await run_in_session(lambda db: ChannelService(db).mark_connected(channel_id))
    return {"ok": True}


async def start_weixin_bind(
    owner_id: str, *, agent_id: Optional[str] = None, local_binding_id: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch the WeChat login QR code, stash the polling context in Redis (TTL), return {bind_id, qrcode_img}.

    Non-empty ``agent_id`` (binding from the subagent page) → verify ownership and carry it
    in the context to the poll phase, where it is written into the connection.
    """
    agent_id = await run_in_session(lambda db: ChannelService(db).admit_bind(owner_id, agent_id))
    adapter = get_adapter("weixin")
    try:
        qr = await adapter.start_qr_bind()
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"获取微信二维码失败：{exc}")
    bind_id = f"wxbind_{uuid.uuid4().hex[:16]}"
    from core.infra.ephemeral import get_ephemeral_state

    await get_ephemeral_state().put(
        f"{_WEIXIN_BIND_PREFIX}{bind_id}",
        json.dumps({"owner_id": owner_id, "qrcode": qr["qrcode"], "agent_id": agent_id, "local_binding_id": local_binding_id}),
        ttl=_WEIXIN_BIND_TTL,
    )
    return {"bind_id": bind_id, "qrcode_img": qr.get("qrcode_img_content", "")}


async def poll_weixin_bind(owner_id: str, bind_id: str) -> Dict[str, Any]:
    """Poll the QR-scan status; confirmed → persist ChannelConnection + start long polling, returns {status, channel_id?}."""
    from core.infra.ephemeral import get_ephemeral_state

    state = get_ephemeral_state()
    key = f"{_WEIXIN_BIND_PREFIX}{bind_id}"
    raw = await state.get(key)
    ctx = json.loads(raw) if raw else None
    if ctx is None or ctx.get("owner_id") != owner_id:
        raise ResourceNotFoundError("weixin_bind", bind_id)
    adapter = get_adapter("weixin")
    try:
        status = await adapter.poll_qr_status(ctx["qrcode"])
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"查询扫码状态失败：{exc}")
    if status.get("status") != "confirmed":
        return {"status": status.get("status") or "waiting"}
    if not isinstance(status.get("bot_token"), str) or not status["bot_token"].strip():
        raise BadRequestError("微信绑定未返回有效凭据，请重新扫码")

    # confirmed → create the connection (app_id is derived from bot_token, satisfying the unique constraint)
    await state.drop(key)
    channel_id, created = await run_in_session(
        lambda db: ChannelService(db).record_weixin_bind(owner_id, ctx, status)
    )
    if created:
        _start_long_conn(channel_id, "weixin")
    return {"status": "confirmed", "channel_id": channel_id}


def _start_long_conn(channel_id: str, channel_type: str) -> None:
    try:
        _manager().start_connection(channel_id, channel_type)
    except Exception:  # noqa: BLE001
        logger.exception("[channels] 启动长连接失败 %s", channel_id)


def _manager():
    from integrations.channels.manager import get_manager

    return get_manager()


GROUP_LISTEN_MODES = ("mention_only", "observe_all")


def _validate_group_listen(channel_type: str, mode: Optional[str]) -> str:
    """Validate the group-listening mode, rejecting it on channels that can never honor it.

    ``observe_all`` only means anything where the platform is capable of delivering non-@
    group messages. Storing it on WeCom/WeChat would leave the owner believing the bot reads
    the group when it structurally cannot — so reject rather than silently accept.
    """
    mode = (mode or "mention_only").strip()
    if mode not in GROUP_LISTEN_MODES:
        raise BadRequestError(f"group_listen_mode 非法：{mode}")
    if mode == "observe_all":
        caps = get_adapter(channel_type).caps
        if not getattr(caps, "supports_group_observe", False):
            raise BadRequestError(f"渠道 {channel_type} 不支持群聊旁听")
    return mode


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

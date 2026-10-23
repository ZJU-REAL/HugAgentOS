"""Unit tests for inbound channel bots (owner service-account model).

Covers: capability flags, repository CRUD, service capability gating + token
lock, resource-whitelist normalization, and the Lark adapter's event
normalization / @-mention stripping / AES decryption / session keying.

No network calls: create_bot's credential validation needs HTTP, so only
the pre-network branches are tested (capability gating / token lock).
"""


from integrations.channels import inbound


from integrations.channels import inbound_attachments


from integrations.channels import inbound_delivery


from integrations.channels import inbound_sessions


from integrations.channels import inbound_state


import asyncio


import base64


import hashlib


import json


import re


import core.runs.events.subscription as subscription


import pytest


from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS, resolve_capabilities


from integrations.channels.adapters.lark import LarkAdapter


from integrations.channels.protocol import InboundMsg, SendResult


from integrations.channels.models import ChannelConnection


from core.db.models import UserShadow


from integrations.channels.repository import ChannelConnectionRepository


from core.infra.exceptions import AccessDeniedError, BadRequestError


from integrations.channels import service as channel_service
from integrations.channels.service import _clean_scope, bot_to_dict


def _share_sessions(monkeypatch, db):
    """Steps that run through run_in_session open their sessions on the test's database."""
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db.get_bind()))


def _mk_user(db, uid="u1", meta=None):
    db.add(UserShadow(user_id=uid, username=uid, extra_data=meta or {}))
    db.commit()


class _FakeConn:
    channel_id = "chan_lark"
    channel_type = "lark"
    app_id = "cli_z"
    config = {}


def _mk_conn(db, owner="u_o", scope=None):
    _mk_user(db, owner, meta={"can_create_channel_bot": True})
    return ChannelConnectionRepository(db).create({
        "channel_id": "chan_in", "owner_user_id": owner, "channel_type": "lark",
        "app_id": "cli_in", "config": {}, "status": "connected", "resource_scope": scope,
    })


def _inbound(conv_id, chat_type, text="hi", mid="m1"):
    return InboundMsg(
        channel_id="chan_in", channel_type="lark", text=text, chat_type=chat_type,
        external_conversation_id=conv_id, sender_id="ou_x", message_id=mid,
    )


class _FakeDTConn:
    channel_id = "chan_dt"
    channel_type = "dingtalk"
    app_id = "dingkey"
    config = {}


def _wecom_conn(token="tk", aes_key_b64=None):
    """Build a fake conn with real Fernet-encrypted credentials."""
    from core.infra.crypto import encrypt_secret

    class _C:
        channel_id = "chan_wc"
        channel_type = "wecom"
        app_id = "corp1"
        config = {
            "app_secret_enc": encrypt_secret("sec"),
            "agent_id_enc": encrypt_secret("1000002"),
            "token_enc": encrypt_secret(token),
            "aes_key_enc": encrypt_secret(aes_key_b64),
        }
    return _C()


def _wecom_encrypt(key32: bytes, msg: str, receiveid: str = "corp1") -> str:
    import struct

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = msg.encode("utf-8")
    plain = b"0123456789abcdef" + struct.pack(">I", len(body)) + body + receiveid.encode()
    pad = 16 - (len(plain) % 16)
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key32), modes.CBC(key32[:16])).encryptor()
    return base64.b64encode(enc.update(plain) + enc.finalize()).decode()


class _FakeWXConn:
    channel_id = "chan_wx"
    channel_type = "weixin"
    app_id = "wx_abc"
    config = {}


def _reg_channel_tool(user_id, chat_id):
    """从原生工具列表取出附件工具函数。"""
    from core.llm.factory.tools.definitions import ToolAssembly
    from integrations.channels.tools.attachment import register_channel_attachment

    tc = ToolAssembly()
    register_channel_attachment(tc, user_id=user_id, chat_id=chat_id)
    return next(tool for tool in tc if tool.name == "channel_read_attachment")._func


def _tool_json(resp):
    block = resp.content[0]
    text = block["text"] if isinstance(block, dict) else getattr(block, "text")
    return json.loads(text)


class _HistAdapter:
    """带 fetch_history 的假 adapter；记录每次调用的 since_ms 以验证增量。"""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def fetch_history(self, conn, conversation_id, *, since_ms, limit, newest_first=False):
        self.calls.append({
            "conv": conversation_id, "since_ms": since_ms,
            "limit": limit, "newest_first": newest_first,
        })
        return self.batches.pop(0) if self.batches else []


def _hist(mid, text, ts_ms, name="群友", attachments=None):
    from integrations.channels.protocol import HistoryItem
    return HistoryItem(
        message_id=mid, sender_name=name, text=text, ts_ms=ts_ms,
        attachments=attachments or [], raw={"lark_message_id": mid},
    )

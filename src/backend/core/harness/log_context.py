"""Shared helpers for call logs: ids, truncation and the current log scope."""
from __future__ import annotations
import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional
from sqlalchemy.orm import Session
from core.db.models import LocalUser, UserShadow
from core.infra.data_masking import mask_sensitive_data
from core.infra.logging import chat_id_var, trace_id_var, user_id_var
from core.infra.logging import get_logger
logger = get_logger(__name__)

def new_id() -> str:
    return uuid.uuid4().hex


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def truncate_text(text: Optional[str], limit: int) -> tuple[Optional[str], bool]:
    if text is None:
        return None, False
    if not isinstance(text, str):
        text = str(text)
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore") + "\n…[truncated]", True


def truncate_json(payload: Any, limit: int) -> tuple[Any, bool]:
    if payload is None:
        return None, False
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode(
            "utf-8", errors="ignore"
        )
    except Exception:
        return {"_repr": str(payload)[:limit]}, True
    if len(encoded) <= limit:
        return payload, False
    preview = encoded[: limit // 2].decode("utf-8", errors="ignore")
    return {"_truncated": True, "_preview": preview}, True


def context_ids() -> Dict[str, str]:
    return {
        "trace_id": trace_id_var.get() or "",
        "user_id": user_id_var.get() or "",
        "chat_id": chat_id_var.get() or "",
    }


def fetch_username(db: Session, user_id: str) -> Optional[str]:
    """The "user name" used in logs: prefer the local account display name
    (nickname → real_name), fall back to the login account username.

    When the audit log panel shows this column, operators should see a
    recognizable person name (e.g. "张三"), not the login account (e.g.
    "zhangsan01") — the latter would not match the display names shown
    elsewhere in the UI.
    """
    if not user_id:
        return None
    try:
        row = (
            db.query(UserShadow.username, LocalUser.nickname, LocalUser.real_name)
            .outerjoin(LocalUser, LocalUser.user_id == UserShadow.user_id)
            .filter(UserShadow.user_id == user_id)
            .one_or_none()
        )
        if not row:
            return None
        return row.nickname or row.real_name or row.username
    except Exception as exc:  # noqa: BLE001
        logger.debug("username_lookup_failed", user_id=user_id, error=str(exc))
        return None


@contextmanager
def subagent_scope(subagent_log_id: Optional[str], source: str = "subagent") -> Iterator[None]:
    tok_id = subagent_log_id_var.set(subagent_log_id)
    tok_src = source_var.set(source)
    try:
        yield
    finally:
        subagent_log_id_var.reset(tok_id)
        source_var.reset(tok_src)


@contextmanager
def skill_scope(source: str = "skill") -> Iterator[None]:
    tok = source_var.set(source)
    try:
        yield
    finally:
        source_var.reset(tok)


def set_current_message_id(message_id: str) -> None:
    current_message_id.set(message_id or "")


def current_subagent_log_id() -> Optional[str]:
    return subagent_log_id_var.get()


def current_source() -> str:
    return source_var.get() or "main_agent"


def effective_status(declared: Optional[str], result: Any) -> str:
    """声明的状态优先，但"成功"要经得起载荷检验。

    只认**顶层** error，且只认 dict / 能解析成 dict 的 JSON 串——正文里出现 error 字样的
    正常内容（比如搜到一篇讲错误码的网页）不该被误判成故障。
    """
    status = declared or "success"
    if status != "success":
        return status
    from core.chat.tool_log import _payload_carries_error

    return "error" if _payload_carries_error(result) else "success"


def sanitize_tool_args(value: Any) -> Any:
    from core.runs.tool_effects.arguments import canonical_tool_args

    if isinstance(value, dict):
        return canonical_tool_args(value)[2]
    return mask_sensitive_data(value, field_patterns=REDACT_FIELDS)


source_var: ContextVar[str] = ContextVar("log_current_source", default="main_agent")


subagent_log_id_var: ContextVar[Optional[str]] = ContextVar(
    "log_current_subagent", default=None
)


current_message_id: ContextVar[str] = ContextVar("log_current_message", default="")


REDACT_FIELDS = [
    s.strip()
    for s in os.getenv(
        "LOG_REDACT_FIELDS",
        "password,token,api_key,apikey,authorization,secret,access_key",
    ).split(",")
    if s.strip()
]


# Shared request correlation survives model gateway token rotation.
model_call_id = ContextVar("desktop_model_call_id", default="")

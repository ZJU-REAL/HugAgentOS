"""Scheduled-task "delivery target" model — generalizes "where to send the result" from
hardcoded channels into configurable multiple targets.

Storage location: ``ScheduledTask.extra_data["delivery_targets"]`` (a list), with
elements like:
    {"type": "inapp"}                                             # in-app: notification center + chat history + sidebar
    {"type": "channel", "channel_id": "chan_…", "conversation_id": "oc_…"}  # IM conversation (Feishu, etc.)
    {"type": "email", "to": "a@b.com"}                            # reserved, delivery not yet implemented

Backward compatibility: legacy tasks put flat ``channel_id`` / ``conversation_id`` only
at the top level of extra_data (old #7 style); ``resolve_delivery_targets`` interprets
this as ``[{inapp}, {channel…}]``. No markers at all → in-app only.

See internal design docs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_VALID_TYPES = ("inapp", "channel", "email")


def resolve_delivery_targets(extra_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolve the final delivery-target list from a task's extra_data (with backward
    compatibility).

    Priority: explicit ``delivery_targets`` > legacy flat ``channel_id/conversation_id``
    > in-app only.
    """
    meta = extra_data if isinstance(extra_data, dict) else {}
    targets = meta.get("delivery_targets")
    if isinstance(targets, list) and targets:
        out: List[Dict[str, Any]] = []
        for t in targets:
            if isinstance(t, dict) and t.get("type") in _VALID_TYPES:
                out.append(dict(t))
        if out:
            return out

    # Backward compatibility: legacy #7 channel tasks only put flat channel_id/conversation_id at the top level
    ch = meta.get("channel_id")
    conv = meta.get("conversation_id")
    legacy: List[Dict[str, Any]] = [{"type": "inapp"}]
    if ch and conv:
        legacy.append({"type": "channel", "channel_id": ch, "conversation_id": conv})
    return legacy


def build_delivery_targets(
    channel_origin: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Automatically build delivery_targets from the current conversation context: a
    channel conversation → [in-app + that channel conversation]; web → [in-app only].

    Explicit delivery targets ("inapp" / a specified channel conversation) are built
    directly by the caller after parsing and do not go through this function.
    """
    origin = channel_origin or {}
    cid = origin.get("channel_id")
    conv = origin.get("conversation_id")
    if cid and conv:
        return [
            {"type": "inapp"},
            {"type": "channel", "channel_id": cid, "conversation_id": conv},
        ]
    return [{"type": "inapp"}]


def has_inapp(targets: List[Dict[str, Any]]) -> bool:
    return any(t.get("type") == "inapp" for t in targets)


def describe_targets(targets: List[Dict[str, Any]]) -> str:
    """Human-readable target description for the agent / user."""
    labels = []
    for t in targets:
        ty = t.get("type")
        if ty == "inapp":
            labels.append("站内")
        elif ty == "channel":
            labels.append("渠道会话")
        elif ty == "email":
            labels.append(f"邮件({t.get('to', '')})")
    return "、".join(labels) or "站内"

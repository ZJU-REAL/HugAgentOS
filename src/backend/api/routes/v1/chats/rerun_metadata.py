"""Restore invocation fields and attachments from saved user messages."""

from typing import Any, Dict, List

from api.schemas import AttachmentItem

_INVOCATION_EXTRA_KEYS = (
    "agent_id",
    "skill_id",
    "skill_name",
    "connector_id",
    "connector_name",
    "plugin_name",
    "plugin_id",
    "mention_agent_id",
    "mention_name",
)


def _restore_invocation(extra: Any) -> Dict[str, Any]:
    """Rebuild the original turn's invocation fields from extra_data."""
    return {key: extra[key] for key in _INVOCATION_EXTRA_KEYS if extra.get(key)}


def _restore_attachments(saved: List[Dict]) -> List[AttachmentItem]:
    """Reconstruct AttachmentItem list from extra_data['attachments'] metadata."""
    return [
        AttachmentItem(
            name=a.get("name", ""),
            mime_type=a.get("mime_type", ""),
            file_id=a.get("file_id", ""),
        )
        for a in saved
        if a.get("file_id")
    ]

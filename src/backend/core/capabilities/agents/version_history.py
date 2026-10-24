"""Version numbers and change history for user-defined agents."""
from __future__ import annotations
from core.capabilities.agents.base_constants import DEFAULT_AGENT_VERSION, VERSIONED_FIELDS
from core.infra.time import utc_now
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from core.db.models import UserAgent

def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return list(value)
    return value


def now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def collect_changed_fields(cls, agent: UserAgent, data: Dict[str, Any]) -> List[str]:
    fields: List[str] = []
    for field in VERSIONED_FIELDS:
        if field not in data:
            continue
        old_value = cls._normalize_value(getattr(agent, field, None))
        new_value = cls._normalize_value(data.get(field))
        if old_value != new_value:
            fields.append(field)
    return fields


def read_version(base_service_type, extra_config: Optional[Dict[str, Any]]) -> str:
    if not isinstance(extra_config, dict):
        return DEFAULT_AGENT_VERSION
    raw = extra_config.get("version")
    return base_service_type._normalize_version(raw if isinstance(raw, str) else "")


def read_change_history(base_service_type, extra_config: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not isinstance(extra_config, dict):
        return []
    raw_history = extra_config.get("change_history")
    if not isinstance(raw_history, list):
        return []

    history: List[Dict[str, str]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        timestamp = item.get("timestamp")
        content = item.get("content")
        version = item.get("version")
        operator_name = item.get("operator_name")
        details = item.get("details")
        if not isinstance(timestamp, str) or not isinstance(content, str):
            continue
        history.append(
            {
                "timestamp": timestamp,
                "content": content,
                "version": base_service_type._normalize_version(
                    version if isinstance(version, str) else ""
                ),
                "operator_name": (
                    operator_name
                    if isinstance(operator_name, str) and operator_name.strip()
                    else "未知用户"
                ),
                "details": base_service_type._normalize_change_details(details),
            }
        )
    return history


def increment_version(base_service_type, version: str) -> str:
    normalized = base_service_type._normalize_version(version)
    match = re.match(r"^[Vv](\d+)\.(\d+)$", normalized)
    if not match:
        return "V1.1"
    major, minor = (int(part) for part in match.groups())
    return f"V{major}.{minor + 1}"


def build_change_summary(changed_fields: List[str], data: Dict[str, Any]) -> str:
    if changed_fields == ["is_enabled"]:
        return "启用了子智能体" if bool(data.get("is_enabled")) else "停用了子智能体"

    changed_labels = [VERSIONED_FIELDS[field] for field in changed_fields]
    if not changed_labels:
        return "更新了智能体配置"
    if len(changed_labels) <= 3:
        return f"修改了{'、'.join(changed_labels)}"
    preview = "、".join(changed_labels[:3])
    return f"修改了{preview}等{len(changed_labels)}项"


def build_change_details(
    cls, agent: UserAgent, changed_fields: List[str], data: Dict[str, Any]
) -> List[Dict[str, str]]:
    details: List[Dict[str, str]] = []
    for field in changed_fields:
        old_value = cls._stringify_detail_value(field, getattr(agent, field, None))
        new_value = cls._stringify_detail_value(field, data.get(field))
        details.append(
            {
                "field": VERSIONED_FIELDS[field],
                "before": old_value,
                "after": new_value,
            }
        )
    return details


def stringify_detail_value(field: str, value: Any) -> str:
    if field == "is_enabled":
        return "启用" if bool(value) else "关闭"
    if value is None:
        return "未填写"
    if isinstance(value, list):
        return "、".join(str(item) for item in value) if value else "未填写"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value).strip()
    return text if text else "未填写"


def normalize_change_details(details: Any) -> List[Dict[str, str]]:
    if not isinstance(details, list):
        return []
    normalized: List[Dict[str, str]] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        before = item.get("before")
        after = item.get("after")
        if not isinstance(field, str):
            continue
        normalized.append(
            {
                "field": field,
                "before": before if isinstance(before, str) else "未填写",
                "after": after if isinstance(after, str) else "未填写",
            }
        )
    return normalized


def normalize_version(version: str) -> str:
    if not isinstance(version, str) or not version.strip():
        return DEFAULT_AGENT_VERSION
    raw = version.strip()
    if re.match(r"^[Vv]\d+\.\d+$", raw):
        return f"V{raw[1:]}"
    legacy_patch = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw)
    if legacy_patch:
        major, minor, patch = (int(part) for part in legacy_patch.groups())
        return f"V{major}.{minor + patch}"
    legacy_minor = re.match(r"^(\d+)\.(\d+)$", raw)
    if legacy_minor:
        major, minor = (int(part) for part in legacy_minor.groups())
        return f"V{major}.{minor}"
    return DEFAULT_AGENT_VERSION


def merge_extra_config(
    current_extra: Optional[Dict[str, Any]],
    incoming_extra: Optional[Dict[str, Any]],
    *,
    version: str,
    change_history: List[Dict[str, str]],
) -> Dict[str, Any]:
    merged = dict(current_extra or {})
    if isinstance(incoming_extra, dict):
        merged.update(incoming_extra)
    merged["version"] = version
    merged["change_history"] = change_history
    return merged

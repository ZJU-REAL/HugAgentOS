"""Validate plugin icons and apply display metadata without marketplace storage."""
from typing import Any, Dict
from core.infra.exceptions import BadRequestError
from core.capabilities.plugins.packaging import definitions as plugin_definitions

def validate_icon(icon: str) -> str:
    """Validate an icon value from the UI picker; returns the stripped value ('' = clear)."""
    icon = (icon or "").strip()
    if not icon:
        return ""
    if icon.startswith("data:"):
        if not icon.startswith("data:image/"):
            raise BadRequestError(message="图标 data URI 必须是 image 类型")
        if len(icon) > plugin_definitions.MAX_ICON_LEN:
            raise BadRequestError(message="图标过大（上传原图请控制在 80KB 以内）")
        return icon
    if not (icon.startswith("/") or icon.startswith("http://") or icon.startswith("https://")):
        raise BadRequestError(message="图标须从图标库选择或上传（不支持任意文本）")
    if len(icon) > plugin_definitions.MAX_ICON_URL_LEN:
        raise BadRequestError(message="图标地址过长")
    return icon


def overlay_market_meta(item: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """Apply effective display metadata onto a market list/detail dict (in place)."""
    if meta.get("display_name"):
        item["name"] = meta["display_name"]
    if meta.get("category"):
        item["category"] = meta["category"]
    if meta.get("icon"):
        item["icon"] = meta["icon"]

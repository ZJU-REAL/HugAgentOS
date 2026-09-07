"""Gateway components that ship bytes instead of a JSON invocation body.

The generic gateway tool only asks whether a component declares an upload
channel; what gets packaged, which endpoint receives it and how the reply is
rewritten belongs to the component. Adding another binary-capable capability
means registering a channel here, not adding a branch to the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

# Wire contract shared by the uploading client and the receiving gateway route.
UPLOAD_OPTIONS_HEADER = "x-capability-upload-options"
UPLOAD_SCHEMA_HEADER = "x-capability-schema"
MAX_UPLOAD_OPTIONS_CHARS = 16000


@dataclass(frozen=True)
class UploadChannel:
    """How one component's tool turns its arguments into an upload."""

    endpoint: str  # gateway sub-path used instead of "call"
    content_type: str
    # (arguments, headers) -> (body bytes, options JSON string)
    package: Callable[[Dict[str, Any], Dict[str, str]], Awaitable[Tuple[bytes, str]]]
    # (result data, cloud base url) -> None; rewrites cloud-relative fields in place
    localize: Optional[Callable[[Dict[str, Any], str], None]] = None


def _site_publish_channel() -> UploadChannel:
    from core.services.desktop_site_publish import localize_site_result, package_local_site

    return UploadChannel(
        endpoint="site-publish",
        content_type="application/gzip",
        package=package_local_site,
        localize=localize_site_result,
    )


# Component base name → its uploading tools. Values are factories so a channel's
# module is imported only when that component is actually invoked.
_CHANNELS: Dict[str, Dict[str, Callable[[], UploadChannel]]] = {
    "site_publish": {"publish_site": _site_publish_channel},
}


def upload_channel(component: str, tool_name: str) -> Optional[UploadChannel]:
    """The declared upload channel for this component's tool, if it has one."""
    factory = _CHANNELS.get(component, {}).get(tool_name)
    return factory() if factory is not None else None


def endpoint_component(endpoint: str) -> Optional[str]:
    """Which component base name a gateway upload endpoint belongs to."""
    for component, tools in _CHANNELS.items():
        for factory in tools.values():
            if factory().endpoint == endpoint:
                return component
    return None

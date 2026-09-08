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


# Providing plugin slug → its uploading tools. The slug is the fact the platform
# registered for the server (``AdminMcpServer.source_plugin``); keying on it means
# a shared and a per-user install of the same plugin resolve identically, and no
# id string ever has to be taken apart. Values are factories so a channel's module
# is imported only when that plugin's tool is actually invoked.
_CHANNELS: Dict[str, Dict[str, Callable[[], UploadChannel]]] = {
    "sites": {"publish_site": _site_publish_channel},
}


def upload_channel(source_plugin: str, tool_name: str) -> Optional[UploadChannel]:
    """The declared upload channel for this plugin's tool, if it has one."""
    factory = _CHANNELS.get(str(source_plugin or ""), {}).get(tool_name)
    return factory() if factory is not None else None


def endpoint_plugin(endpoint: str) -> Optional[str]:
    """Which providing plugin a gateway upload endpoint belongs to."""
    for slug, tools in _CHANNELS.items():
        for factory in tools.values():
            if factory().endpoint == endpoint:
                return slug
    return None

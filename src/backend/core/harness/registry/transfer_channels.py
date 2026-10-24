"""Gateway components that ship bytes instead of a JSON invocation body.

The generic gateway tool only asks whether a component declares an upload
channel; what gets packaged, which endpoint receives it and how the reply is
rewritten belongs to the component. Adding another binary-capable capability
means registering a channel here, not adding a branch to the transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class UploadReceiver:
    options_model: Any
    max_bytes: int
    publish: Callable[..., Any]


def register_upload_channel(source_plugin, tool_name, factory, *, receiver=None):
    registration_state().CHANNELS.setdefault(source_plugin, {})[tool_name] = factory
    if receiver is not None:
        registration_state().RECEIVERS[factory().endpoint] = receiver


def register_result_localizer(source_plugin, tool_name, localize):
    registration_state().LOCALIZERS.setdefault(source_plugin, {})[tool_name] = localize


def upload_receiver(endpoint):
    receiver = registration_state().RECEIVERS.get(endpoint)
    if receiver is None:
        raise RuntimeError("gateway upload component is not registered")
    return receiver


def upload_channel(source_plugin: str, tool_name: str) -> Optional[UploadChannel]:
    """The declared upload channel for this plugin's tool, if it has one."""
    factory = registration_state().CHANNELS.get(str(source_plugin or ""), {}).get(tool_name)
    return factory() if factory is not None else None


def endpoint_plugin(endpoint: str) -> Optional[str]:
    """Which providing plugin a gateway upload endpoint belongs to."""
    for slug, tools in registration_state().CHANNELS.items():
        for factory in tools.values():
            if factory().endpoint == endpoint:
                return slug
    return None


def result_localizer(source_plugin: str, tool_name: str):
    """Resolve component-owned receipt handling independently of upload transport."""
    return registration_state().LOCALIZERS.get(source_plugin, {}).get(tool_name)


@dataclass
class RegistrationState:
    CHANNELS: Dict[str, Dict[str, Callable[[], UploadChannel]]] = field(default_factory=dict)
    LOCALIZERS: Dict[str, Dict[str, Callable[..., Any]]] = field(default_factory=dict)
    RECEIVERS: Dict[str, UploadReceiver] = field(default_factory=dict)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS

"""Inbound channel bot integration framework (owner service-account model).

External IMs (Feishu first, then DingTalk / WeCom) push messages in → normalize
→ reuse the chat orchestration pipeline as the bot owner → send the reply back to
the original channel. Unified abstractions ``ChannelAdapter`` + ``InboundMsg`` /
``SendResult``, one adapter per channel; abstract first, then add channels.

See internal design docs.
"""

from core.channels.protocol import (
    ChannelAdapter,
    ChannelCaps,
    InboundMsg,
    SendResult,
)
from core.channels.registry import get_adapter, register_adapter, list_adapters

__all__ = [
    "ChannelAdapter",
    "ChannelCaps",
    "InboundMsg",
    "SendResult",
    "get_adapter",
    "register_adapter",
    "list_adapters",
]

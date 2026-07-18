"""Channel adapter protocol + normalized data classes.

Each external IM channel implements one ``ChannelAdapter``: normalize the channel
protocol into ``InboundMsg`` (inbound) and send agent replies back via ``push``
(outbound, returning ``SendResult``). Capability differences (message length cap,
markdown support, long-connection support) are declared explicitly via
``ChannelCaps``, which the upper layer uses to degrade/chunk — avoiding
"if channel name" checks everywhere.

Benchmarked against Hermes-Agent ``BasePlatformAdapter`` + capability flags /
OpenClaw ``defineChannelMessageAdapter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ChannelCaps:
    """Channel capability declaration. The upper layer uses this to chunk/degrade instead of hardcoding per-channel branches in business logic."""

    channel_type: str
    # Maximum characters per message (overflow is split into multiple messages by the upper layer). 0 = unlimited.
    max_message_len: int = 0
    supports_markdown: bool = False
    # Whether the adapter splits over-long messages itself (True) or the upper layer must (False)
    splits_long_messages: bool = False
    # Whether a backend-resident long connection (WebSocket / long polling) is supported. False = webhook only.
    supports_long_conn: bool = True
    # Bind mode: 'credentials' (fill in an App ID/Secret form) | 'qr' (QR-scan device flow, e.g. WeChat iLink).
    # The frontend uses this to decide between showing a credential form or a QR button — avoiding "if channel name" in the frontend.
    bind_mode: str = "credentials"
    # Credential form field names (only used when bind_mode='credentials'): the frontend renders inputs dynamically from this.
    # Conventional fields: app_id / app_secret are the two core columns; the rest (encrypt_key/verification_token/
    # agent_id/token/aes_key etc.) pass through via CreateBotRequest.extra, get encrypted, and merge into config.
    credential_fields: tuple = ("app_id", "app_secret")


# Outbound send error classification (benchmarked against Hermes SendResult.error_kind). Lets the upper layer decide retry/degrade/alert.
SEND_ERROR_KINDS = (
    "too_long",      # content too long (should be chunked and resent)
    "bad_format",    # format not accepted
    "forbidden",     # no permission / bot not in the conversation
    "rate_limited",  # rate limit hit
    "transient",     # temporary network/server error (retryable)
    "unknown",
)


@dataclass
class InboundMsg:
    """Normalized inbound message (every channel converges to this structure)."""

    channel_id: str                       # internal channel_connections.channel_id
    channel_type: str                     # 'lark' | ...
    text: str                             # user text (envelope such as @bot already stripped)
    chat_type: str                        # 'p2p' | 'group'
    external_conversation_id: str         # p2p=speaker identifier / group=group identifier, used for session keying
    sender_id: str = ""                   # speaker open_id (audit only, not resolved to a platform account)
    sender_name: str = ""                 # speaker nickname (audit)
    message_id: str = ""                  # channel-side message ID (idempotent dedup)
    # Inbound attachments (files/images): each item {kind: 'file'|'image', key: <resource key>, name: <filename>}.
    # Parsed by the adapter; inbound orchestration downloads from it → stores an Artifact → injects into uploaded_files for the agent to read.
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)  # raw event, kept for reference


@dataclass
class SendResult:
    """Normalized outbound send result."""

    success: bool
    message_id: Optional[str] = None
    error_kind: Optional[str] = None      # one of SEND_ERROR_KINDS
    error_detail: Optional[str] = None
    # Message IDs of the follow-up chunks when an over-long message was split
    continuation_ids: List[str] = field(default_factory=list)

    @classmethod
    def ok(cls, message_id: Optional[str] = None) -> "SendResult":
        return cls(success=True, message_id=message_id)

    @classmethod
    def fail(cls, error_kind: str, detail: Optional[str] = None) -> "SendResult":
        kind = error_kind if error_kind in SEND_ERROR_KINDS else "unknown"
        return cls(success=False, error_kind=kind, error_detail=detail)


# Long-connection event callback: adapter receives a raw event → normalizes → calls this to hand the InboundMsg to the upper layer.
InboundCallback = Callable[[InboundMsg], Awaitable[None]]


def chunk_text(text: str, max_len: int) -> List[str]:
    """Split long text into chunks per the channel's single-message cap, breaking at newlines/spaces where possible (avoiding mid-word cuts).

    ``max_len<=0`` is treated as unlimited → return a single chunk as-is.
    """
    text = text or ""
    if max_len <= 0 or len(text) <= max_len:
        return [text] if text else []
    chunks: List[str] = []
    rest = text
    while len(rest) > max_len:
        window = rest[:max_len]
        cut = window.rfind("\n")
        if cut < int(max_len * 0.6):          # no newline near the end → fall back to finding a space
            sp = window.rfind(" ")
            cut = sp if sp >= int(max_len * 0.6) else max_len
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


@runtime_checkable
class ChannelAdapter(Protocol):
    """Channel adapter protocol. One implementation per channel, registered into the registry.

    ``conn`` is a ``ChannelConnection`` ORM row (the adapter fetches decrypted credentials
    internally as needed).
    """

    caps: ChannelCaps

    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        """Signature verification in webhook mode. Always True in long-connection mode."""
        ...

    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        """Channel event → InboundMsg. Non-message events (e.g. url_verification / read receipts) return None."""
        ...

    async def push(self, conn: Any, inbound: InboundMsg, content: str) -> SendResult:
        """Send the agent reply back to the source conversation (auto-chunks over-long content; first message_id + continuation_ids)."""
        ...

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        """Send a single text message (no chunking) — called on demand by placeholder/streaming orchestration."""
        ...

    async def edit_message(self, conn: Any, message_id: str, text: str) -> SendResult:
        """Edit an already-sent text message (used for the "thinking → final reply" placeholder update)."""
        ...

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        """Validate credentials at bind time (exchange for an access_token, etc.); returns a bot identity summary. Raises on failure."""
        ...

    # ── Files (optional; channels without file support may skip these — inbound orchestration checks hasattr) ──────
    async def download_resource(
        self, conn: Any, inbound: InboundMsg, attachment: Dict[str, Any]
    ) -> Optional[bytes]:
        """Download an inbound attachment's binary content."""
        ...

    async def push_file(
        self, conn: Any, inbound: InboundMsg, content: bytes, filename: str, mime_type: str
    ) -> SendResult:
        """Send a file generated by the agent back to the source conversation."""
        ...

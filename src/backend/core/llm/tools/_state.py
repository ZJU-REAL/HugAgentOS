"""Per-chat read history shared by Read / Edit / Write tools.

Claude Code on the host uses ``readFileState`` (Map<path, ReadState>) to guarantee two
invariants:
- **Edit / Write must Read first** — otherwise it may blindly write without knowing the current content
- **Not externally modified** — if the file was changed by another process after the Read, Edit/Write errors out to make the model Read again

In the sandbox we can't rely on mtime (it's lost on sandbox restart, and the OpenSandbox SDK
may not expose stat), so we use a SHA-256 content fingerprint for the "not externally changed"
check, which is more robust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ReadEntry:
    """Record of a single Read."""

    content: bytes           # full bytes obtained on the last Read (only meaningful for a full read)
    sha256: str              # content fingerprint
    offset: Optional[int]    # None = full read; non-None = partial read (Edit/Write not allowed)
    limit: Optional[int]
    # True = this Read returned the **parsed text** of a binary document (docx/pdf/xlsx/pptx),
    # not the raw bytes. Edit/Write must reject and clearly say so (don't let the model think
    # "just do a full read again" will unlock it — that would treat the binary document as plain
    # text and corrupt it on overwrite).
    parsed_doc: bool = False


class ReadStateTracker:
    """Per-chat singleton tracking which files have been Read this conversation.

    Instance lifetime: a single agent creation (i.e. one workflow turn). chat_id provides a
    natural partition — multiple tool calls within the same chat share one tracker; different
    chats are independent.
    """

    def __init__(self) -> None:
        self._m: dict[str, ReadEntry] = {}

    def record(self, path: str, entry: ReadEntry) -> None:
        self._m[path] = entry

    def get(self, path: str) -> Optional[ReadEntry]:
        return self._m.get(path)

    def is_full_read(self, path: str) -> bool:
        e = self._m.get(path)
        return e is not None and e.offset is None

    def forget(self, path: str) -> None:
        self._m.pop(path, None)

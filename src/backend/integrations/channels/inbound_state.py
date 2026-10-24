"""Channel state responsibilities."""
from __future__ import annotations
import asyncio
from collections import OrderedDict, defaultdict

MAX_INBOUND_FILE_BYTES = 50 * 1024 * 1024


conv_locks: "defaultdict[str, asyncio.Lock]" = defaultdict(asyncio.Lock)


SEEN_MAX = 4096


seen_message_ids: "OrderedDict[str, None]" = OrderedDict()


RESET_COMMANDS = frozenset({
    "/new", "/clear", "/reset", "/restart",
    "新对话", "新会话", "清空", "清空上下文", "清除上下文",
    "重置", "重置对话", "重新开始",
})


OBSERVE_MAX = 60


OBSERVE_TEXT_MAX = 500


OBSERVE_FILE_INDEX_MAX = 100


HISTORY_COLDSTART_WINDOW_H = 168


HISTORY_MAX_MESSAGES = 80


HISTORY_MAX_TOTAL_CHARS = 12000


HISTORY_TIMEOUT_S = 10


HISTORY_MAX_FAILURES = 3

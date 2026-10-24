"""Default version id and caches for prompt versions."""
from __future__ import annotations
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Optional

DEFAULT_VERSION_ID = "default"


TURBO_LAST_RESORT = (
    "你是极速查询助手。收到问题后，最多发起 1-2 轮工具调用（同一轮内可并行"
    "调用多个检索工具），拿到结果立即用中文简洁作答，并标注信息来源。"
    "不要使用超出检索范围的工具，不要反复迭代。"
)


@dataclass
class PromptVersionsState:
    payload_cache: Optional[Dict[str, Any]] = None
    payload_cache_key: Optional[tuple] = None
    payload_cache_lock: object = field(default_factory=lambda: Lock())
    fs_parts_cache: Dict[str, tuple] = field(default_factory=lambda: {})
    fs_parts_cache_lock: object = field(default_factory=lambda: Lock())


_STATE = PromptVersionsState()


def process_state() -> PromptVersionsState:
    return _STATE

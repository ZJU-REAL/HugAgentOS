"""prompt versions state responsibilities."""
from __future__ import annotations
from threading import Lock
from typing import Any, Dict, Optional

DEFAULT_VERSION_ID = "default"

















TURBO_LAST_RESORT = (
    "你是极速查询助手。收到问题后，最多发起 1-2 轮工具调用（同一轮内可并行"
    "调用多个检索工具），拿到结果立即用中文简洁作答，并标注信息来源。"
    "不要使用超出检索范围的工具，不要反复迭代。"
)


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class PromptVersionsStateState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    payload_cache: Optional[Dict[str, Any]] = None
    payload_cache_key: Optional[tuple] = None
    payload_cache_lock: object = state_field(default_factory=lambda: Lock())
    fs_parts_cache: Dict[str, tuple] = state_field(default_factory=lambda: {})
    fs_parts_cache_lock: object = state_field(default_factory=lambda: Lock())

@state_cache(maxsize=1)
def process_state() -> PromptVersionsStateState:
    return PromptVersionsStateState()

# Initialize under the import lock before concurrent callers share the owner.
process_state()

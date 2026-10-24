"""Per-chat citation anchor allocator and registry."""
from __future__ import annotations
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Sequence
import logging
from core.llm.citations import anchor_state
from core.llm.citations import types

logger = logging.getLogger(__name__)

class AnchorAllocator:
    """会话内单调锚点计数器 + 按 tool_id 的引用注册表。

    并发安全性：分配是同步代码段（无 await 点），asyncio 并行工具任务
    通过 ContextVar 共享同一实例，不会重号。
    """

    def __init__(self, start: int = 1) -> None:
        self._next = max(1, int(start or 1))
        self._by_tool_id: Dict[str, List[types.AnchorCitation]] = {}
        self.all: List[types.AnchorCitation] = []

    def next_id(self) -> str:
        aid = f"e{self._next}"
        self._next += 1
        return aid

    def register(self, tool_id: Optional[str], items: Sequence[types.AnchorCitation]) -> None:
        if not items:
            return
        self.all.extend(items)
        if tool_id:
            self._by_tool_id.setdefault(str(tool_id), []).extend(items)

    def citations_for(self, tool_id: Optional[str]) -> List[types.AnchorCitation]:
        if not tool_id:
            return []
        return list(self._by_tool_id.get(str(tool_id), []))


def attach_allocator(agent: Any, allocator: AnchorAllocator) -> AnchorAllocator:
    """run 入口调用：把发号器绑定到 agent（并写 ContextVar 供子链路兜底）。"""
    try:
        setattr(agent, ALLOC_ATTR, allocator)
    except Exception as exc:  # noqa: BLE001 - pydantic 模型等禁止动态属性时降级到 ContextVar
        logger.debug("[citation-anchor] attach to agent failed: %s", exc)
    CITATION_ALLOCATOR.set(allocator)
    return allocator


def resolve_allocator(agent: Any) -> AnchorAllocator:
    """中间件调用：取本 run 的发号器；缺失时就地建一个并绑定，保证始终可用。"""
    allocator = getattr(agent, ALLOC_ATTR, None)
    if isinstance(allocator, AnchorAllocator):
        return allocator
    allocator = CITATION_ALLOCATOR.get()
    if not isinstance(allocator, AnchorAllocator):
        allocator = AnchorAllocator()
    return attach_allocator(agent, allocator)


def collect_citation_dicts(
    tool_id: Optional[str],
    allocator: Optional[AnchorAllocator] = None,
) -> List[Dict[str, Any]]:
    """编排层统一取引用入口（dict 形态，直接可进 SSE / trace / 落库）。

    发号器是**唯一**编号方：中间件在结果回给模型前已完成注号并按 tool_id 登记，
    这里只做精确取回，不做二次提取。``allocator`` 由调用方显式传入（run 入口绑定
    在 agent 上的那一个）；不传时退到 ContextVar（子智能体链路）。
    """
    if allocator is None:
        allocator = CITATION_ALLOCATOR.get()
    if allocator is None:
        return []
    return [item.to_dict() for item in allocator.citations_for(tool_id)]


def anchor_start_for_chat(chat_id: Optional[str]) -> int:
    """跨轮续号：扫描该会话已落库消息的 citations，取最大锚点 + 1。

    任何异常都回退 1（引用功能降级，不影响对话）。
    """
    if not chat_id:
        return 1
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatMessage

        max_seen = 0
        with SessionLocal() as db:
            rows = (
                db.query(ChatMessage.extra_data)
                .filter(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.chat_seq.desc())
                .limit(200)
                .all()
            )
        for (extra,) in rows:
            for cit in (extra or {}).get("citations") or []:
                m = anchor_state.ANCHOR_RE.match(str(cit.get("id", "")) if isinstance(cit, dict) else "")
                if m:
                    max_seen = max(max_seen, int(m.group(1)))
        return max_seen + 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("[citation-anchor] start scan failed for chat %s: %s", chat_id, exc)
        return 1


CITATION_ALLOCATOR: ContextVar[Optional[AnchorAllocator]] = ContextVar(
    "jx_citation_allocator", default=None
)


ALLOC_ATTR = "_jx_citation_allocator"

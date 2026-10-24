"""Annotate tool results with citation anchors."""
from __future__ import annotations
import json
from typing import List, Optional, Tuple
import logging
from core.llm.citations.allocator import AnchorAllocator
from core.llm.citations import anchor_state
from core.llm.citations import extraction
from core.llm.citations import types

logger = logging.getLogger(__name__)

def annotate_tool_result(
    tool_name: str,
    tool_id: Optional[str],
    text: str,
    allocator: AnchorAllocator,
) -> Tuple[str, List[types.AnchorCitation]]:
    """给一份工具结果文本注号。

    返回 (注号后的文本, 登记的引用条目)。不可注号（跳过名单 / 空结果 /
    解析失败且判定无引用价值）时原样返回 (text, [])。**绝不抛错**。
    """
    if not text or tool_name in anchor_state.SKIP_TOOLS:
        return text, []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    try:
        if isinstance(parsed, dict):
            items = extraction.extract_and_inject(tool_name, tool_id, parsed, allocator)
            if not items:
                return text, []
            return json.dumps(parsed, ensure_ascii=False), items
        if isinstance(parsed, list):
            # 裸数组结果：包壳后按启发式处理，回注仍输出裸数组语义（保持兼容 → 整份 1 条）
            parsed = {"items": parsed}
            items = extraction.extract_and_inject(tool_name, tool_id, parsed, allocator)
            if not items:
                return text, []
            return json.dumps(parsed["items"], ensure_ascii=False), items
        # 纯文本结果：整份 1 条，文末追加锚点行
        spec = anchor_state.TOOL_SPECS.get(tool_name)
        if spec and spec.mode == "list":
            # 声明为列表型但拿到纯文本 → 形态异常，放行不注号
            return text, []
        aid = allocator.next_id()
        item = types.AnchorCitation(
            id=aid,
            tool_name=tool_name,
            tool_id=tool_id,
            title=extraction.display_title(tool_name, spec),
            url="",
            snippet=str(text)[:anchor_state.SNIPPET_MAX],
            source_type=(spec.source_type if spec else "unknown"),
            item_index=-1,
        )
        return f"{text}\n\n[cite_id: {aid}]", [item]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[citation-anchor] annotate failed tool=%s: %s", tool_name, exc)
        return text, []

"""Length limits applied to message content and thinking before they are stored."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import logging
logger = logging.getLogger(__name__)

CONTENT_MAX = 100_000


TRUNC_NOTE = "\n\n…（本条回复过长，已截断保存；完整过程见工具调用记录）"


THINKING_MAX = 200_000


def clamp_message_content(content: str) -> str:
    """Apply the database content limit independently of ChatService wiring."""

    text = content or ""
    if len(text) <= CONTENT_MAX:
        return text
    keep = CONTENT_MAX - len(TRUNC_NOTE)
    logger.warning("[chat] message content truncated: %d -> %d chars", len(text), CONTENT_MAX)
    kept = text[:keep]
    # 老格式的思考是以 <think>…</think> 内联在正文里的。截断点可能落在块中间，
    # 闭合标签一丢，历史重建就把整段思考当正文渲染出来（思考泄露到页面上）。
    close = "</think>"
    if kept.count("<think>") > kept.count(close):
        kept = kept[: keep - len(close)] + close
    return kept + TRUNC_NOTE


def clamp_thinking(
    blocks: Optional[List[Dict[str, Any]]],
    segments: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
    """思考单独存一列，有自己的额度，不再和正文抢 content 的 10 万字上限。

    超额时从最早的块开始丢——最后几轮的推理离最终答案最近，对用户回看最有用。

    ``metadata.segments`` 按下标引用思考块，丢块会让下标错位，所以两者必须一起夹取：
    返回夹取后的块列表，以及下标已重映射、引用了被丢块的段落已剔除的段落表。
    """
    if not blocks:
        return None, segments or None
    if sum(len(str(b.get("content") or "")) for b in blocks) <= THINKING_MAX:
        return blocks, segments or None
    kept: List[Dict[str, Any]] = []
    kept_indices: List[int] = []
    budget = THINKING_MAX
    for offset, block in enumerate(reversed(blocks)):
        text = str(block.get("content") or "")
        if not text:
            continue
        if len(text) > budget:
            text = text[len(text) - budget :]
        if not text:
            break
        budget -= len(text)
        kept.append({**block, "content": text})
        kept_indices.append(len(blocks) - 1 - offset)
        if budget <= 0:
            break
    kept.reverse()
    kept_indices.reverse()
    if not kept:
        return None, drop_thinking_segments(segments, {})
    remap = {old: new for new, old in enumerate(kept_indices)}
    return kept, drop_thinking_segments(segments, remap)


def drop_thinking_segments(
    segments: Optional[List[Dict[str, Any]]],
    remap: Dict[int, int],
) -> Optional[List[Dict[str, Any]]]:
    """把段落表里的思考下标重映射到夹取后的位置，引用已丢块的段落直接剔除。"""
    if not segments:
        return segments or None
    rebuilt: List[Dict[str, Any]] = []
    for segment in segments:
        if segment.get("type") != "thinking":
            rebuilt.append(segment)
            continue
        new_index = remap.get(segment.get("index"))
        if new_index is None:
            continue
        rebuilt.append({**segment, "index": new_index})
    return rebuilt or None

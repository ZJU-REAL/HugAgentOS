# -*- coding: utf-8 -*-
"""工具结果「展示副本」的体积上限——唯一真源。

裁的只是**推给浏览器的那一份**：

- 模型侧永远拿完整内容（超长由 AgentScope 的截断 + ``core/llm/offloader`` 负责，
  与这里无关）；
- 数据库里 ``ChatMessage.tool_calls`` 也存完整的一份，审计和事后回查不受影响。

为什么必须裁：``read_file`` 单次可以返回 5MB 文本，这 5MB 过去会原样进 SSE、进浏览器
内存、进聊天树，此后每次重渲染都要再遍历一遍。实测这足以把标签页拖垮。

**裁剪必须保持 JSON 结构不变。** 前端的专用渲染器（知识库列表、搜索结果、通用列表卡片）
是按字段名认结构的，把结果整体换成一段预览字符串会让它们统统退化成裸 JSON——那是
用户可见的功能倒退。所以这里只削**超长的字符串叶子**，键、层级、数组长度一律不动。

阈值取得很宽（单个字段 5 万字、整份结果 30 万字）：正常的检索结果、报告、代码都远
在其下，完全不会被碰到；只有"把一整个大文件塞进结果"这种病态情况才会命中。
"""
from __future__ import annotations

from typing import Any, Tuple

# 单个字符串字段推给前端的上限（字符数）。
DISPLAY_STRING_MAX = 50_000
# 整份结果里所有字符串加起来的上限。超出之后剩余字段按 DISPLAY_TAIL_STRING_MAX 收紧，
# 避免"一万个各 4 万字的字段"这种绕过单字段上限的形态。
DISPLAY_TOTAL_MAX = 300_000
# 总预算耗尽后，后续字符串字段还能保留多少。
DISPLAY_TAIL_STRING_MAX = 2_000
# 递归深度上限：结果来自外部 MCP，不能假设它不会自引用/嵌套过深。
DISPLAY_MAX_DEPTH = 24


_LIVE_NOTE = "模型已拿到全文，完整副本也已存入本条消息的记录"
_HISTORY_NOTE = "展开这张工具卡即可按需取回完整内容"


def _clip(text: str, keep: int, note: str) -> str:
    """截到 keep 字，并在末尾写清楚被截了多少——别让用户以为这就是全部。"""
    total = len(text)
    return (
        text[:keep]
        + f"\n\n〔内容过长，此处只显示前 {keep:,} 字；完整内容共 {total:,} 字，{note}〕"
    )


# 历史列表（GET /messages）用的**更紧**的一档：翻一页就是几十条消息，这里按的是
# "先给个够认出内容的梗概，展开工具卡时再回后端取全文"。见 HISTORY_ 系列常量。
HISTORY_STRING_MAX = 2_000
HISTORY_TOTAL_MAX = 20_000


def bound_result_for_display(
    value: Any,
    *,
    string_max: int = DISPLAY_STRING_MAX,
    total_max: int = DISPLAY_TOTAL_MAX,
    tail_string_max: int = DISPLAY_TAIL_STRING_MAX,
    note: str = _LIVE_NOTE,
) -> Tuple[Any, bool]:
    """返回 (裁剪后的展示副本, 是否发生过裁剪)。

    入参不会被修改：命中裁剪的分支会重建对象，没命中的分支原样复用引用。
    """
    state = {"budget": total_max, "clipped": False}

    def walk(node: Any, depth: int) -> Any:
        if depth > DISPLAY_MAX_DEPTH:
            return node
        if isinstance(node, str):
            keep = string_max if state["budget"] > 0 else tail_string_max
            state["budget"] -= len(node)
            if len(node) <= keep:
                return node
            state["clipped"] = True
            return _clip(node, keep, note)
        if isinstance(node, dict):
            out = {}
            changed = False
            for key, item in node.items():
                new_item = walk(item, depth + 1)
                if new_item is not item:
                    changed = True
                out[key] = new_item
            return out if changed else node
        if isinstance(node, (list, tuple)):
            items = [walk(item, depth + 1) for item in node]
            if all(new is old for new, old in zip(items, node)):
                return node
            return items if isinstance(node, list) else tuple(items)
        return node

    return walk(value, 0), state["clipped"]


def bound_result_for_history(value: Any) -> Tuple[Any, bool]:
    """历史列表用的紧档裁剪：只留够认出内容的梗概。

    完整正文由 ``GET /v1/chats/{chat_id}/messages/{message_id}/tool-calls/{tool_id}``
    在用户展开工具卡时按需回取——翻一页历史不该顺带把几十份大结果一起搬进浏览器。
    """
    return bound_result_for_display(
        value,
        string_max=HISTORY_STRING_MAX,
        total_max=HISTORY_TOTAL_MAX,
        tail_string_max=HISTORY_STRING_MAX // 4,
        note=_HISTORY_NOTE,
    )

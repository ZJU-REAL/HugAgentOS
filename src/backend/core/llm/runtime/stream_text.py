"""Split visible answer text from thinking and detect tool error text."""
from __future__ import annotations
from typing import Tuple
from core.infra.model_output import THINK_BLOCK_RE
from core.llm.runtime import streaming_constants

def looks_like_tool_error(content: str) -> bool:
    if not content:
        return False
    s = content.strip()
    if not s:
        return False
    head = s[:64]
    if s.startswith(('{"error"', "{'error'", '{"ok": false', '{"ok":false')):
        return True
    if '"ok": false' in head or '"ok":false' in head:
        return True
    if s.startswith(("Error executing tool", "Error: ", "Traceback (most recent call last)")):
        return True
    if "validation error for" in head:
        return True
    if "不存在或已删除" in s or "无权访问" in head:
        return True
    if streaming_constants.HTTP_ERR_RE.search(s):
        return True
    return False


def strip_thinking_answer(raw: str, enable_thinking: bool, in_thinking: bool) -> Tuple[str, bool]:
    """Extract the user-visible "answer" portion from the accumulated raw text.

    Returns (answer, new_in_thinking). When enable_thinking=True, returns as-is (the frontend
    parses <think> itself); otherwise suppresses every <think>…</think> span and returns only
    the visible text.

    一次模型调用里可以出现**多段**思考链（ReAct 模型常在工具调用前再想一轮）。
    早先的实现只取"最后一个 </think> 之后的内容"：当第二段思考刚开了 <think>
    还没闭合时，rfind 命中的是第一段的闭标签，于是 ``vis1<think>正在生成的思考…``
    被整段当成正文吐出去 —— 网页端 enable_thinking=False 只在快速模式出现（模型
    本来就不思考），而定时任务这条路径恰恰是"模型照常思考、正文里不该有思考"，
    于是思考过程就跟着落进了自动化任务的输出正文。

    改成"先删掉所有闭合块，再处理孤儿标签"，未闭合的那段思考不会再漏出来。
    """
    if enable_thinking:
        return raw, False
    # 1) 闭合的思考块整段丢掉（可能有多段）
    text = THINK_BLOCK_RE.sub("", raw)
    # 2) 孤儿闭标签：有些服务端把开标签吃进 chat template，正文以裸思考开头，
    #    最后一个 </think> 之前的都是思考。
    last_end = text.rfind("</think>")
    if last_end != -1:
        text = text[last_end + len("</think>") :]
        return text, False
    # 3) 还没闭合的思考块：它之后的内容都还在思考里，不出正文
    open_idx = text.find("<think>")
    if open_idx != -1:
        return text[:open_idx], True
    # 4) 整段没有任何标签：沿用上一次的判定（服务端预填开标签的形态下，
    #    在 </think> 到来之前只能靠这一位把裸思考压住）
    if in_thinking:
        return "", True
    return text, False

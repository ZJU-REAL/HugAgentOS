"""enable_thinking=False 时的思考链抑制。

这条路径只有"模型照常思考、正文里不该出现思考"的场景会走到 —— 主要是定时任务
（automation_scheduler 把 enable_thinking 置 False，但没有把 chat_mode 降到 fast，
所以模型仍然产出思考链）。抑制一旦漏，思考过程就直接落进自动化任务的输出正文。
"""

from orchestration.streaming import _strip_thinking_answer


def strip(raw: str, in_thinking: bool = False):
    return _strip_thinking_answer(raw, False, in_thinking)


def test_closed_block_is_dropped():
    assert strip("<think>推理</think>正文") == ("正文", False)


def test_second_unclosed_block_does_not_leak():
    """ReAct 模型会在工具调用前再想一轮。

    旧实现取"最后一个 </think> 之后的内容"，第二段思考刚开标签还没闭合时命中的
    是第一段的闭标签，于是还在生成的思考链被整段当成正文吐出去。
    """
    assert strip("<think>推理一</think>正文一<think>推理二还没写完") == ("正文一", True)


def test_multiple_closed_blocks_keep_all_visible_text():
    assert strip("<think>一</think>正文一<think>二</think>正文二") == ("正文一正文二", False)


def test_orphan_closing_tag_keeps_only_the_answer():
    """有的服务端把开标签吃进 chat template，补全从裸思考开始。"""
    assert strip("裸思考</think>正文") == ("正文", False)


def test_unclosed_first_block_emits_nothing():
    assert strip("<think>只写了一半") == ("", True)


def test_untagged_text_passes_through():
    assert strip("普通回答") == ("普通回答", False)


def test_untagged_continuation_stays_suppressed_while_in_thinking():
    assert strip("还在想", in_thinking=True) == ("", True)


def test_enable_thinking_passes_raw_through():
    raw = "<think>推理</think>正文"
    assert _strip_thinking_answer(raw, True, False) == (raw, False)

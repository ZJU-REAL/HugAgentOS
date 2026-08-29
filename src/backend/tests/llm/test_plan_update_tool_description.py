"""``update_plan`` 工具描述必须自带步骤推进的节奏说明。

2026-08-14 压缩 prefill（commit 7578469f）把这段挪进系统提示词后，长任务里模型开头调
两次就再也不更新——工具描述贴着模型选工具的那一刻，系统提示词那节够不着。用测试钉住，
下次再瘦身会当场红。

工具描述由 AgentScope 从函数 docstring 生成，这里用桩 toolkit 直接读 docstring，不绑定
框架某个版本的注册接口。
"""

import pytest

from core.llm.plan_update_tool import register_plan_update_tool


class _CapturingToolkit:
    def __init__(self) -> None:
        self.functions: dict = {}

    def register_tool_function(self, fn, **_kwargs) -> None:
        self.functions[fn.__name__] = fn


@pytest.fixture()
def update_plan_doc() -> str:
    toolkit = _CapturingToolkit()
    register_plan_update_tool(toolkit)
    fn = toolkit.functions.get("update_plan")
    assert fn is not None, "update_plan 没有注册进 toolkit"
    return fn.__doc__ or ""


def test_description_keeps_the_step_cadence(update_plan_doc):
    """讲清什么时候该标 completed，否则模型只会在开头调一次。"""
    assert "做下一步之前" in update_plan_doc
    assert "completed" in update_plan_doc


def test_description_allows_batching_completions(update_plan_doc):
    """一遍做完多步时允许一次性收——逼模型逐步补会让它拿改计划替代干活。"""
    assert "一次性全部标成 completed" in update_plan_doc


def test_rationale_goes_to_explanation_not_title(update_plan_doc):
    """title 是计划栏展示给用户的标题，改动理由必须走独立的 explanation 参数。"""
    assert "explanation" in update_plan_doc
    assert "不要写进改动理由" in update_plan_doc


def test_explanation_is_a_real_parameter():
    """理由必须是工具签名里的形参，不能借用 title。"""
    import inspect

    toolkit = _CapturingToolkit()
    register_plan_update_tool(toolkit)
    params = inspect.signature(toolkit.functions["update_plan"]).parameters
    assert "explanation" in params
    assert params["explanation"].default == ""


def test_explanation_never_reaches_the_plan_bar_payload():
    """理由混进 plan_update 载荷就会污染用户可见的计划栏。"""
    from core.llm.plan_update_tool import parse_plan_update_args

    parsed = parse_plan_update_args({
        "title": "做 PPT",
        "explanation": "因为第 4 步拆得太粗",
        "steps": [{"title": "读方案", "status": "completed"}],
    })
    assert parsed == {"title": "做 PPT", "steps": [{"title": "读方案", "status": "completed"}]}


def test_description_still_says_it_does_not_interrupt(update_plan_doc):
    """不说明"不会打断"，模型会把它当成需要等待确认的闸。"""
    assert "不会打断" in update_plan_doc


def test_steps_parameter_still_demands_the_full_list(update_plan_doc):
    """全量语义丢了就会退化成增量补丁，计划栏会被覆盖成残缺清单。"""
    assert "全量列表" in update_plan_doc


def test_return_echoes_the_canonical_checklist():
    """回传权威清单（deepseek-harness 形式）——模型才不会对着过期的提醒文本猜当前状态。"""
    import asyncio

    toolkit = _CapturingToolkit()
    register_plan_update_tool(toolkit)
    resp = asyncio.run(toolkit.functions["update_plan"](
        steps=[
            {"title": "读方案", "status": "completed"},
            {"title": "填内容页", "status": "in_progress"},
            {"title": "交付", "status": "pending"},
        ],
        title="做 PPT",
    ))
    text = resp.content[0]["text"] if isinstance(resp.content[0], dict) else resp.content[0].text
    assert "1 待办，1 进行中，1 已完成" in text
    assert "1. [completed] 读方案" in text
    assert "2. [in_progress] 填内容页" in text
    assert "3. [pending] 交付" in text


def test_description_forbids_resubmitting_an_unchanged_plan(update_plan_doc):
    """线上长任务里模型卡在同一步时会每轮重发同一份清单，白烧一整轮上下文。"""
    assert "状态确实发生变化时才调用" in update_plan_doc


def test_prompt_section_forbids_resubmitting_an_unchanged_plan():
    """系统提示词那节和工具描述要给出同一条口径，否则两处互相抵消。"""
    from core.llm.plan_update_tool import build_plan_update_prompt_section

    assert "状态确实发生变化时才调用" in build_plan_update_prompt_section()

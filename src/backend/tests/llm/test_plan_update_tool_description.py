"""``update_plan`` 的工具描述里必须留着"每完成一步立即再调用一次"。

这条看着像和系统提示词「任务计划清单」节重复的冗余文案，2026-08-14 压缩 prefill
（commit 7578469f，工具 schema 13.8k→10.8k tokens）时就是这么被挪走的。后果是
长任务里模型开头调两次 ``update_plan`` 就再也不更新——工具描述贴着模型做工具选择的
那一刻，而系统提示词那一节躺在两万多 token 的静态前缀里，够不着。

所以用一条测试把它钉死：下次再有人做 prefill 瘦身，删掉这句会当场红。

工具描述是 AgentScope 从函数 docstring 生成的，这里直接用一个只负责接住函数的桩
toolkit 去读 docstring —— 不绑定 AgentScope 某个版本的注册接口，测试才不会因为
框架升级而假红。
"""

import pytest

from core.llm.plan_update_tool import register_plan_update_tool


class _CapturingToolkit:
    """只接住被注册的函数，不做任何 schema 生成。"""

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


def test_description_keeps_the_per_step_cadence(update_plan_doc):
    """描述里必须同时讲清"每完成一步"和"立即再调用"，否则模型只会在开头调一次。"""
    assert "每完成一步" in update_plan_doc
    assert "立即再调用一次" in update_plan_doc


def test_description_still_says_it_does_not_interrupt(update_plan_doc):
    """另一条同样不能省：不说明"不会打断"，模型会把它当成需要等待确认的闸。"""
    assert "不会打断" in update_plan_doc


def test_steps_parameter_still_demands_the_full_list(update_plan_doc):
    """全量语义丢了就会退化成增量补丁，计划栏会被一步步覆盖成残缺清单。"""
    assert "全量列表" in update_plan_doc

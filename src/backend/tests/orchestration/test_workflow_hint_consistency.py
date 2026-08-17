"""工作流模式提示段必须与 run_job 的真实默认值一致。

背景（HugAgentOS 线上实测，trace 82febb85）：`run_job` 的 `wait` 默认值已从 `True` 改成
`False`（后台跑），工具 docstring 也跟着改了，但 `agent_factory._WORKFLOW_MODE_HINT`
里那句"`wait=True`（默认）会一直等到作业结束"没人动。系统提示的权重压过工具 docstring，
模型照着提示里的"默认"照抄传了 `wait=True` —— 一次 265 项的分类作业把整轮 SSE 阻塞了
397 秒，用户看到的就是"工具卡片一直在前台转圈，说好的后台跑呢"。改了默认值等于没改。

同一段提示里"唤醒间隔默认 15 分钟"也是旧的（实际 `_PROGRESS_WAKE_EVERY_S = 300`）。

这类漂移的共性：**同一个事实在代码和提示词里各存了一份，改一处不会让另一处报错**。
所以这里把两份钉在一起——提示词怎么描述随便写（判据、语气、举例都可以调），但凡是它
陈述的"默认值"，必须和代码里的真值对得上。

用 ast 读源码而不是 import：这些常量藏在 agent_factory / job_tool 里，import 会拖起
agentscope 与半个后端，而这条断言只关心字面量。
"""

import ast
import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]


def _module_constant(rel_path: str, name: str):
    """取模块顶层 `name = <字面量>` 的值（相邻字符串字面量由解析器自动拼接）。"""
    tree = ast.parse((_BACKEND / rel_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{rel_path} 里找不到顶层常量 {name}")


def _run_job_arg_default(arg_name: str):
    """取 job_tool.register_run_job 内层 `async def run_job(...)` 的某个参数默认值。"""
    tree = ast.parse((_BACKEND / "core/llm/tools/job_tool.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_job":
            args = node.args.args
            defaults = node.args.defaults
            # 默认值右对齐到参数列表尾部
            offset = len(args) - len(defaults)
            for idx, arg in enumerate(args):
                if arg.arg == arg_name:
                    assert idx >= offset, f"run_job 的 {arg_name} 没有默认值"
                    return ast.literal_eval(defaults[idx - offset])
    raise AssertionError("job_tool.py 里找不到 async def run_job")


@pytest.fixture(scope="module")
def hint() -> str:
    return _module_constant("core/llm/agent_factory.py", "_WORKFLOW_MODE_HINT")


def test_wait_default_is_background():
    """作业默认后台跑。改这条要连带改提示段与 docstring，别只改签名。"""
    assert _run_job_arg_default("wait") is False


def test_hint_states_the_real_wait_default(hint: str):
    """提示段陈述的 wait 默认值必须与签名一致。

    只校验"默认"这件事，不限制提示段怎么给判断标准——让模型按作业规模自己选前台/后台
    是刻意的设计，措辞可以随便调，但不能把默认值说反。
    """
    real_default = _run_job_arg_default("wait")
    assert "`wait` 默认" in hint, "提示段必须明说 wait 的默认值，否则模型只能猜"

    # "wait=True（默认）" / "wait=true 默认" 这类把 True 说成默认的写法，正是踩过的坑
    claims_true_is_default = re.search(r"`?wait\s*=\s*[Tt]rue`?\s*（默认", hint)
    claims_false_is_default = re.search(r"`wait`\s*默认\s*`?false`?", hint, re.IGNORECASE)
    if real_default is False:
        assert not claims_true_is_default, "签名默认 false，提示段却把 wait=true 说成默认值"
        assert claims_false_is_default, "签名默认 false，提示段没有如实写出来"
    else:  # pragma: no cover - 默认值真被改回 True 时这条才生效
        assert claims_true_is_default and not claims_false_is_default


def test_hint_progress_interval_matches_runtime(hint: str):
    """提示段里的唤醒间隔必须等于 job_runtime 的真值（曾漂移成 15 分钟）。"""
    seconds = _module_constant("orchestration/job_runtime.py", "_PROGRESS_WAKE_EVERY_S")
    stated = re.search(r"默认\s*(\d+)\s*分钟[^）]*`progress_wake_sec`", hint)
    assert stated, "提示段应说明默认唤醒间隔（写成「默认 N 分钟，`progress_wake_sec` 可调」）"
    assert int(stated.group(1)) * 60 == int(
        seconds
    ), f"提示段写默认 {stated.group(1)} 分钟，但 _PROGRESS_WAKE_EVERY_S={seconds} 秒"


def test_docstring_agrees_with_hint_on_default():
    """工具 docstring 与提示段不能各说各话——模型两边都看得见。"""
    src = (_BACKEND / "core/llm/tools/job_tool.py").read_text(encoding="utf-8")
    assert "**默认 false = 后台跑**" in src, "docstring 没写清 wait 默认后台跑"
    assert not re.search(r"``wait=True``（默认）", src), "docstring 把 wait=True 说成默认值"

"""Deterministic routing for explicit natural-language sub-agent commands."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class ExplicitSubagentCommand:
    """A safely resolved one-turn direct handoff."""

    agent_id: str
    agent_name: str
    task: str


_COMMAND_PREFIXES = ("请调用", "调用")
_OPTIONAL_AGENT_SUFFIXES = ("子智能体", "智能体")
_LEADING_CONNECTORS = ("请帮我", "帮我", "请", "来", "去")
_TASK_OPENERS = (
    "分析",
    "查询",
    "核查",
    "核验",
    "评估",
    "生成",
    "检查",
    "搜索",
    "整理",
    "比较",
    "计算",
    "执行",
    "处理",
    "调查",
    "判断",
    "识别",
    "提取",
    "汇总",
    "验证",
    "审查",
    "对",
)
_DISCUSSION_OPENERS = ("是否", "能否", "可否", "怎么", "如何", "为什么", "是什么")
_SEPARATORS = " \t\r\n:：,，。;；-—"
_QUOTES = (("「", "」"), ("“", "”"), ('"', '"'), ("'", "'"))


def _extract_task_after_name(body: str, name: str) -> Optional[str]:
    consumed = -1
    for opening, closing in _QUOTES:
        token = f"{opening}{name}{closing}"
        if body.startswith(token):
            consumed = len(token)
            break
    if consumed < 0 and body.startswith(name):
        consumed = len(name)
    if consumed < 0:
        return None

    tail = body[consumed:]
    suffix_consumed = False
    if not name.endswith(_OPTIONAL_AGENT_SUFFIXES):
        for suffix in _OPTIONAL_AGENT_SUFFIXES:
            if tail.startswith(suffix):
                tail = tail[len(suffix) :]
                suffix_consumed = True
                break

    # An unquoted name without a suffix needs a clear token boundary. This
    # prevents an agent named "搜索" from matching "搜索助手".
    if not suffix_consumed and tail and tail[0] not in _SEPARATORS:
        return None

    task = tail.lstrip(_SEPARATORS)
    for connector in _LEADING_CONNECTORS:
        if task.startswith(connector) and len(task) > len(connector):
            task = task[len(connector) :].lstrip(_SEPARATORS)
            break
    if not task or task.startswith(_DISCUSSION_OPENERS):
        return None
    if not task.startswith(_TASK_OPENERS):
        return None
    return task


def parse_explicit_subagent_command(
    message: str,
    available_agents: Iterable[Mapping[str, Any]],
) -> Optional[ExplicitSubagentCommand]:
    """Resolve a strict ``调用 <exact agent> <task>`` command.

    This parser intentionally requires the command at the beginning, one
    unique enabled exact agent name, and an action-oriented task. A sentence
    that merely discusses an agent, such as ``调用 X 是否合适``, stays on the
    main-agent route.
    """
    text = (message or "").strip()
    body = ""
    for prefix in _COMMAND_PREFIXES:
        if text.startswith(prefix):
            body = text[len(prefix) :].lstrip()
            break
    if not body:
        return None

    agents = [
        item
        for item in available_agents
        if item.get("agent_id") and item.get("name") and item.get("is_enabled", True)
    ]
    name_counts = Counter(str(item["name"]) for item in agents)
    agents.sort(key=lambda item: len(str(item["name"])), reverse=True)

    for agent in agents:
        name = str(agent["name"])
        if name_counts[name] != 1:
            continue
        task = _extract_task_after_name(body, name)
        if task:
            return ExplicitSubagentCommand(
                agent_id=str(agent["agent_id"]),
                agent_name=name,
                task=task,
            )
    return None

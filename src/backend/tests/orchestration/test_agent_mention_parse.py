"""``@agent`` mention parsing: longest name wins, message order is preserved.

Agent names that share a prefix (``搜索`` / ``搜索助手``, ``bot`` / ``botanist``)
must not produce a false mention of the shorter name, otherwise the prompt hint
built from the result tells the main model to call an agent nobody mentioned.
"""

from __future__ import annotations

from core.agent_runtime.targets import _parse_agent_mentions as parse

_AGENTS_CN = [
    {"name": "搜索", "agent_id": "A"},
    {"name": "搜索助手", "agent_id": "B"},
]
_AGENTS_EN = [
    {"name": "bot", "agent_id": "X"},
    {"name": "botanist", "agent_id": "Y"},
]


def test_long_name_does_not_pull_in_shorter_prefix():
    assert parse("@搜索助手 帮我查一下", _AGENTS_CN) == ["B"]


def test_short_name_alone_still_matches():
    assert parse("@搜索 今天天气", _AGENTS_CN) == ["A"]


def test_mentions_follow_message_order():
    assert parse("@搜索助手 然后 @搜索", _AGENTS_CN) == ["B", "A"]
    assert parse("@搜索 接着 @搜索助手", _AGENTS_CN) == ["A", "B"]


def test_bare_word_without_at_is_not_a_mention():
    assert parse("帮我搜索一下天气", _AGENTS_CN) == []


def test_duplicate_mentions_are_deduplicated():
    assert parse("@搜索 @搜索 hi", _AGENTS_CN) == ["A"]


def test_empty_and_nameless_inputs_are_safe():
    assert parse("@anything", []) == []
    assert parse("", _AGENTS_CN) == []
    assert parse("@搜索", [{"name": "", "agent_id": "Z"}]) == []
    assert parse("@搜索", [{"agent_id": "Z"}]) == []


def test_latin_prefix_shadow_obeys_longest_match():
    assert parse("Hi @bot please", _AGENTS_EN) == ["X"]
    assert parse("Hi @botanist please", _AGENTS_EN) == ["Y"]

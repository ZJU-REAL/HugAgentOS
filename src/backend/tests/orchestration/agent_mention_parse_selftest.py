"""Selftest: ``@agent`` mention parsing rejects prefix-shadow false positives.

Background
----------
``routing/workflow.py:_parse_agent_mentions`` parses ``@<agent_name>``
segments in the user message into a "list of subagent IDs the user has
explicitly asked to call". Downstream:

* ``routing/workflow.py`` uses it for fallback display-name resolution of
  ``call_subagent`` on the tool_call card (when no agent_id is found, the
  first mentioned agent is used).
* ``core.llm.subagent_tool.build_subagent_prompt_section`` renders this ID
  list into a system prompt section appended to the main agent's prompt —
  ``**用户已指定调用子智能体：X、Y。请直接使用 call_subagent ...**``.

The old implementation looked like this:

    for agent in available_agents:
        name = agent.get("name", "")
        pattern = f"@{re.escape(name)}"
        if pattern in message:
            mentioned.append(agent["agent_id"])

``re.escape`` is meaningless here — the check below is
``str.__contains__``, not a regex match. The real landmine is that ``in``
does plain substring containment: when two agent names share a prefix,
e.g. ``搜索`` and ``搜索助手``, and the user types ``@搜索助手``:

    "@搜索" in "@搜索助手 hi"  →  True   # ← false positive
    "@搜索助手" in "@搜索助手 hi"  →  True

The resulting prompt section would read *"用户已指定调用子智能体：搜索、搜索助手"*,
explicitly instructing the main agent to call a sibling agent that was never
@-mentioned — it either calls the wrong agent outright or wastes a
tool_call. Chinese scenarios hit this almost unavoidably — "搜索 / 搜索助手",
"分析 / 分析师", "翻译 / 翻译官" are all common prefix-overlapping names.
English is bitten by the same mine, e.g. ``bot`` / ``botanist``.

Fix
---
Iterate names in descending ``name`` length, recording already-matched
``(start, end)`` character spans; a shorter prefix only counts as a hit
when it appears at a position **not occupied by a longer name**. This way
``@搜索助手`` counts only as ``搜索助手``; ``@搜索`` counts as ``搜索``
only when it appears in a standalone position. Finally, sort results by
position in the message so the name order in the downstream prompt section
matches the order the user wrote.

Test strategy
-------------
The import chain at the top of ``routing/workflow.py`` is heavy (anyio /
agentscope / DB engine …); running the selftest in a plain dev env without
those installed would ImportError immediately. Here we use ``ast`` to carve
the source of ``_parse_agent_mentions`` out and ``exec`` it in a clean
namespace, completely bypassing workflow.py's import side effects — same
trick as ``batch_user_msg_dedup_selftest.py``.

Run directly:

    python3 -m tests.agent_mention_parse_selftest
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable, List

_WORKFLOW_PY = Path(__file__).resolve().parents[2] / "routing" / "workflow.py"


def _load_parse_agent_mentions() -> Callable[[str, list], list]:
    """Pull just ``_parse_agent_mentions`` out of routing/workflow.py.

    Avoids triggering the file's full import chain (anyio, agentscope,
    DB engine, …) which may not be installed in lightweight test envs.
    """
    src = _WORKFLOW_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_agent_mentions":
            ns: dict = {}
            exec(ast.unparse(node), ns)
            return ns["_parse_agent_mentions"]
    raise AssertionError("_parse_agent_mentions not found in routing/workflow.py")


_AGENTS_CN = [
    {"name": "搜索", "agent_id": "A"},
    {"name": "搜索助手", "agent_id": "B"},
]
_AGENTS_EN = [
    {"name": "bot", "agent_id": "X"},
    {"name": "botanist", "agent_id": "Y"},
]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_long_name_does_not_pull_in_shorter_prefix_cn() -> None:
    """``@搜索助手`` must NOT also report ``搜索`` as mentioned."""
    fn = _load_parse_agent_mentions()
    got = fn("@搜索助手 帮我查一下", _AGENTS_CN)
    assert got == ["B"], (
        f"prefix-shadow regression: '@搜索助手' should only mention B, got {got!r}. "
        "If both A and B come back, the substring `'@搜索' in message` false-positive "
        "is back — the downstream prompt section would tell the LLM to call both agents."
    )


def test_short_name_alone_still_matches_cn() -> None:
    """``@搜索`` (no '助手' after it) must still resolve to the ``搜索`` agent."""
    fn = _load_parse_agent_mentions()
    got = fn("@搜索 今天天气", _AGENTS_CN)
    assert got == ["A"], f"expected ['A'], got {got!r}"


def test_both_mentions_preserve_message_order() -> None:
    """Both agents mentioned → result follows the order they appear in the message."""
    fn = _load_parse_agent_mentions()
    got = fn("@搜索助手 然后 @搜索", _AGENTS_CN)
    assert got == ["B", "A"], (
        f"expected ['B', 'A'] (message order), got {got!r}. "
        "Iteration-order results make the prompt hint read wrong: the LLM may "
        "assume the first listed agent is the primary target."
    )
    got2 = fn("@搜索 接着 @搜索助手", _AGENTS_CN)
    assert got2 == ["A", "B"], f"expected ['A', 'B'] (message order), got {got2!r}"


def test_no_at_means_no_mention() -> None:
    """A bare word ``搜索`` (no '@') must NOT count as a mention."""
    fn = _load_parse_agent_mentions()
    got = fn("帮我搜索一下天气", _AGENTS_CN)
    assert got == [], (
        f"expected [] (no '@' in the message), got {got!r}. "
        "Without the '@' prefix the user is talking about the topic, not calling "
        "the agent — false-positive mentions inject misleading prompt hints."
    )


def test_duplicate_mentions_deduplicated() -> None:
    """``@搜索 @搜索`` lists ``搜索`` exactly once."""
    fn = _load_parse_agent_mentions()
    got = fn("@搜索 @搜索 hi", _AGENTS_CN)
    assert got == ["A"], f"expected ['A'] (deduped), got {got!r}"


def test_empty_inputs_safe() -> None:
    fn = _load_parse_agent_mentions()
    assert fn("@anything", []) == []
    assert fn("", _AGENTS_CN) == []
    # Agent dict without 'name' must be skipped silently
    assert fn("@搜索", [{"name": "", "agent_id": "Z"}]) == []
    assert fn("@搜索", [{"agent_id": "Z"}]) == []


def test_prefix_shadow_avoidance_latin() -> None:
    """``@bot`` vs ``@botanist`` — Latin script must obey the same longest-match rule."""
    fn = _load_parse_agent_mentions()
    # Plain @bot followed by a space — only the short one
    assert fn("Hi @bot please", _AGENTS_EN) == ["X"], "plain '@bot' must match only bot"
    # @botanist — only the long one, not also bot
    got = fn("Hi @botanist please", _AGENTS_EN)
    assert got == ["Y"], (
        f"prefix-shadow regression (Latin): '@botanist' must only mention Y, got {got!r}"
    )


def test_function_does_not_use_regex_module() -> None:
    """The fix should drop the no-op ``import re`` — ``re.escape`` was never
    doing anything because the surrounding check is substring containment,
    not a regex match. Keeping a dead import wastes review attention and
    makes future readers think regex is involved.
    """
    src = _WORKFLOW_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_agent_mentions":
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        assert alias.name != "re", (
                            "_parse_agent_mentions still has a stale `import re` — "
                            "re.escape is unnecessary for a substring check and the "
                            "fix replaced the body with str.find()-based scanning"
                        )
                if isinstance(child, ast.ImportFrom) and child.module == "re":
                    raise AssertionError(
                        "_parse_agent_mentions still imports from `re` — not needed after the fix"
                    )
            return
    raise AssertionError("_parse_agent_mentions not found in routing/workflow.py")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_all() -> int:
    cases: List[tuple] = [
        ("'@搜索助手' must NOT also mention shorter '搜索'",
         test_long_name_does_not_pull_in_shorter_prefix_cn),
        ("'@搜索' alone still resolves to the '搜索' agent",
         test_short_name_alone_still_matches_cn),
        ("multiple mentions follow message order",
         test_both_mentions_preserve_message_order),
        ("bare '搜索' (no '@') is not a mention",
         test_no_at_means_no_mention),
        ("duplicate @mentions are deduplicated",
         test_duplicate_mentions_deduplicated),
        ("empty / malformed inputs are safe",
         test_empty_inputs_safe),
        ("Latin '@bot' vs '@botanist' obeys longest-match",
         test_prefix_shadow_avoidance_latin),
        ("_parse_agent_mentions no longer imports `re`",
         test_function_does_not_use_regex_module),
    ]
    print("=== agent_mention_parse_selftest ===")
    failed = 0
    for label, fn in cases:
        try:
            fn()
        except AssertionError as exc:
            print(f"  ✗ {label}\n    → {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {label}\n    → unexpected error: {exc!r}")
            failed += 1
        else:
            print(f"  ✓ {label}")
    if failed:
        print(f"=== agent_mention_parse_selftest: FAILED ({failed}/{len(cases)}) ===")
        return 1
    print("=== agent_mention_parse_selftest: OK ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())

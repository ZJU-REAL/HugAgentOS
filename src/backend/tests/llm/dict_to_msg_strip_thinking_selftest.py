"""Selftest: `dict_to_msg` strips `<think>...</think>` from historical
assistant messages before feeding them to agent.memory.

Background
----------
The SSE main path's streaming save writes the **raw** streaming output
(including `<think>` tags) into `chat_messages.content` — that is the source
data for the frontend's "thinking process" panel, so it must not be stripped
at SAVE time. But when the next turn feeds that assistant message back into
agent.memory, `<think>...</think>` becomes pure pollution:

1. **Budget bloat**: on Qwen3 / DeepSeek R1 the thinking block is often 5–10x
   the answer's length, eating several K extra tokens per turn.
2. **Format mismatch**: `<think>` appearing in a historical assistant position
   matches no chat-template's training distribution; the model tries to parse
   it as a structured tag, and either imitates the format by regurgitating its
   own think block, or flatly misreads it as "this is my previous tool-call
   record".
3. **Echo amplification**: the model recites the previous turn's thinking as
   part of the answer.
4. **Summarizer pollution**: when compaction feeds the history to the summary
   model, thinking blocks dilute or even drown out the real answer content.

The non-streaming path (`run_chat_workflow`) has long called
`strip_thinking()` before saving (see `routing/workflow.py:284`), so
non-streaming chat's DB content is clean; streaming chat is dirty — the same
`dict_to_msg` loads both kinds of chat with inconsistent behavior.

Fix
---
In `dict_to_msg`, call `strip_thinking()` once for messages with
`role == "assistant"` and string `content`. This is the convergence point of
all cross-turn loading paths:

- `routing/streaming.py` (main chat, streaming)
- `routing/workflow.py` (main chat, non-streaming)
- `routing/subagents/plan_mode.py` (plan-execution steps)
- `core/llm/subagent_tool.py` (shared-context subagent)

One fix, all four paths benefit.

Test strategy
-------------
Zero third-party dependencies: use `ast` to extract `strip_thinking` and
`dict_to_msg` from `message_compat.py` and exec them in an isolated namespace,
avoiding `from agentscope...` dragging in the whole pydantic / openai / mem0
import chain. `Msg` is stubbed with a lightweight `SimpleNamespace` factory
that only cares about the `role` / `content` fields.

How to run:

    python3 -m tests.dict_to_msg_strip_thinking_selftest
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace


_COMPAT_PY = (
    Path(__file__).resolve().parents[2]
    / "core" / "llm" / "message_compat.py"
)


# ---------------------------------------------------------------------------
# Function extraction (avoid agentscope import chain)
# ---------------------------------------------------------------------------


def _extract_function_source(src: str, name: str) -> str:
    """Return the source of the top-level function named *name*."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"function {name!r} not found in message_compat.py")


def _build_isolated_namespace() -> dict:
    """Build a namespace where `dict_to_msg` and `strip_thinking` can run.

    Stubs out `Msg` so we don't need agentscope at import time.
    """
    src = _COMPAT_PY.read_text(encoding="utf-8")
    strip_thinking_src = _extract_function_source(src, "strip_thinking")
    dict_to_msg_src = _extract_function_source(src, "dict_to_msg")

    def _msg_stub(name, content, role):
        return SimpleNamespace(name=name, content=content, role=role)

    ns: dict = {
        "Msg": _msg_stub,
        # dict_to_msg uses typing imports indirectly via annotations; provide
        # `Dict` and `Any` so the AST exec doesn't NameError.
        "Dict": dict,
        "Any": object,
        # AS2.0's dict_to_msg wraps str into a block list via _wrap_content;
        # this selftest's contract only cares about strip_thinking behavior, so
        # an identity stub keeps content as a bare string and the assertions
        # unaffected by wrapping.
        "_wrap_content": lambda c: c,
    }

    # strip_thinking has no annotations beyond `str` — exec in same ns.
    exec(compile(strip_thinking_src, "<strip_thinking>", "exec"), ns)
    exec(compile(dict_to_msg_src, "<dict_to_msg>", "exec"), ns)
    return ns


_NS = _build_isolated_namespace()
dict_to_msg = _NS["dict_to_msg"]
strip_thinking = _NS["strip_thinking"]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_assistant_with_think_block_stripped() -> None:
    """Qwen3 / DeepSeek R1 style `<think>...</think>answer` must keep only the answer."""
    raw = "<think>用户问的是 Q3 营收，我应该查数据库</think>2025 Q3 营收 32.1 亿元。"
    expected = "2025 Q3 营收 32.1 亿元。"
    msg = dict_to_msg({"role": "assistant", "content": raw})
    assert msg.role == "assistant"
    assert msg.content == expected, (
        f"assistant content not stripped, got: {msg.content!r}"
    )


def test_assistant_with_open_think_only_passthrough() -> None:
    """With only `<think>` and no `</think>`, keep the original text to avoid deleting legitimate content.

    `strip_thinking` only cuts when `</think>` is present. This protects
    instructional answers like "let me demonstrate the `<think>` tag...".
    """
    text = "下面演示 <think> 标签的用法，但句子没结束。"
    msg = dict_to_msg({"role": "assistant", "content": text})
    assert msg.content == text


def test_assistant_with_no_think_passthrough() -> None:
    """A plain answer (no think block) must be kept as-is."""
    text = "2025 Q3 营收为 32.1 亿元，同比 +12%。"
    msg = dict_to_msg({"role": "assistant", "content": text})
    assert msg.content == text


def test_user_role_not_stripped() -> None:
    """If `<think>` appears in a user message (unlikely but legal), it must not be stripped."""
    text = "请解释 `<think>thinking</think>` 这个标签的作用"
    msg = dict_to_msg({"role": "user", "content": text})
    assert msg.role == "user"
    assert msg.content == text, "user content must never be touched by think-strip"


def test_system_role_not_stripped() -> None:
    """System messages (e.g. frozen memory block) are kept as-is."""
    text = "[session_memory_frozen]\n<think>some seed</think>\nrules…"
    msg = dict_to_msg({"role": "system", "content": text})
    assert msg.role == "system"
    assert msg.content == text


def test_assistant_multimodal_list_content_untouched() -> None:
    """Multimodal list[block] content (not a pollution source via the SSE save path) is left untouched."""
    blocks = [
        {"type": "text", "text": "<think>x</think>answer"},
        {"type": "image", "url": "..."},
    ]
    msg = dict_to_msg({"role": "assistant", "content": blocks})
    # list content is passed through unchanged (the only string-based pollution
    # path is the streaming save).
    assert msg.content == blocks


def test_assistant_empty_content_safe() -> None:
    """Empty content neither raises nor changes."""
    msg = dict_to_msg({"role": "assistant", "content": ""})
    assert msg.content == ""

    msg2 = dict_to_msg({"role": "assistant"})
    assert msg2.content == ""


def test_assistant_multiple_think_blocks_strips_to_last() -> None:
    """With multiple think blocks, `strip_thinking` keeps the content after the last `</think>`.

    Matches the existing contract of
    `core/llm/message_compat.py::strip_thinking`: for complex model output
    interleaving thinking + tool calls + final answer, what you end up with is
    the final answer.
    """
    msg = dict_to_msg({
        "role": "assistant",
        "content": (
            "<think>第一步思考</think>"
            "中间产物\n"
            "<think>第二步思考</think>"
            "最终答案"
        ),
    })
    assert msg.content == "最终答案"


def test_assistant_think_then_whitespace_lstripped() -> None:
    """`strip_thinking` includes an `.lstrip()` that removes newlines/spaces after `</think>`."""
    msg = dict_to_msg({
        "role": "assistant",
        "content": "<think>analysis</think>\n\n  实际回答",
    })
    assert msg.content == "实际回答"


def test_role_mapping_preserved() -> None:
    """`human` / `ai` role mapping + think-strip coexist."""
    msg = dict_to_msg({
        "role": "ai",
        "content": "<think>analysis</think>final",
    })
    assert msg.role == "assistant"
    assert msg.content == "final"

    msg2 = dict_to_msg({"role": "human", "content": "<think>x</think>"})
    assert msg2.role == "user"
    # User role: not stripped.
    assert msg2.content == "<think>x</think>"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    cases = [
        ("assistant content with </think> is stripped", test_assistant_with_think_block_stripped),
        ("open <think> without close passes through", test_assistant_with_open_think_only_passthrough),
        ("plain assistant content untouched", test_assistant_with_no_think_passthrough),
        ("user role never stripped", test_user_role_not_stripped),
        ("system role never stripped", test_system_role_not_stripped),
        ("multimodal list content untouched", test_assistant_multimodal_list_content_untouched),
        ("empty / missing content safe", test_assistant_empty_content_safe),
        ("multiple think blocks → last only", test_assistant_multiple_think_blocks_strips_to_last),
        ("trailing whitespace after </think> lstripped", test_assistant_think_then_whitespace_lstripped),
        ("role mapping (ai/human) + strip coexist", test_role_mapping_preserved),
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in cases:
        try:
            fn()
            print(f"✓ {name}")
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"✗ {name}: {exc!r}")

    if failures:
        raise SystemExit(
            f"\n{len(failures)} test(s) failed out of {len(cases)}"
        )
    print(f"\nAll {len(cases)} tests passed.")


if __name__ == "__main__":
    _run_all()

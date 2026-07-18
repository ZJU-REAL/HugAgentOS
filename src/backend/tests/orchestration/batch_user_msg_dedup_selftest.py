"""Selftest: batch execution of a single item no longer adds the user prompt to agent memory twice.

Background
----------
The old implementation of `_run_item_via_workflow` in `routing/batch_orchestrator.py`
had a passage like this:

    try:
        await agent.memory.add(Msg("user", prompt, "user"))

        async for event_type, payload in streaming_agent.stream(
            session_messages=[{"role": "user", "content": prompt}],
            ...
        ):

`StreamingAgent.stream()` itself pops the trailing user message off `session_messages`
(see `routing/streaming.py:159-171`), constructs `user_msg = Msg(prompt)`, then calls
`agent.reply(user_msg)`. AgentScope's `ReActAgent.reply(user_msg)` pushes `user_msg`
into `agent.memory` inside reply (the comment at `routing/workflow.py:263-264` on the
main chat path spells this contract out: "Load history EXCLUDING the last user message —
agent.reply() will add it to memory internally, avoiding duplicates").

So the `agent.memory.add(Msg("user", prompt, "user"))` line above is a pure duplicate:
after one run, `agent.memory` contains two **adjacent user messages with identical content**.

Consequences
------------
1. The OpenAI / Qwen formatters feed both to the model, i.e. a duplicate prompt is
   stuffed in before the first reasoning round — wasting context window for nothing.
2. Tool-call prompt cache hit rates get scrambled (two consecutive identical user
   messages are not in the cache samples).
3. Hooks like `file_context_pre_reply` that "find the most recent user message" see the
   manually-added copy **first**, and only **then** the one `reply()` is about to add; if
   the hook rewrites it (merging image blocks, adding history digests), it modifies the
   first copy, while the unmodified second copy is what actually enters ReAct reasoning —
   silent data loss.
4. The batch-task "trajectory → admin_skill_draft" distillation pipeline reads chat
   history and learns the duplicate user messages as multi-turn dialogue.

Fix
---
Simply delete that `agent.memory.add(...)` line and rely on `streaming_agent.stream()`
→ `agent.reply()` to add user_msg to memory once. Also remove the inline
`from agentscope.message import Msg` import that becomes dead code as a result.

This test has zero third-party dependencies — it parses the batch_orchestrator.py source
directly with `ast` and verifies:

1. The body of `_run_item_via_workflow` contains **no** call to
   `agent.memory.add(Msg("user", prompt, ...))`.
2. The synthesis fallback in `_fallback_recover_final_text` **still keeps**
   `agent.memory.add(Msg("user", synthesis_hint, ...))` — that one is necessary
   (it precedes a no-argument `agent.reply()` call; reply won't add the msg by itself).
3. The `streaming_agent.stream(session_messages=...)` call is still present in
   `_run_item_via_workflow`.

If anyone writes that manual add back in the future, they will hang on this regression pin.

Run directly:

    python3 -m tests.batch_user_msg_dedup_selftest
"""

from __future__ import annotations

import ast
from pathlib import Path

_BATCH_PY = Path(__file__).resolve().parents[2] / "routing" / "batch_orchestrator.py"


def _load_source() -> str:
    return _BATCH_PY.read_text(encoding="utf-8")


def _find_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async function {name!r} not found in batch_orchestrator.py")


def _calls_in(node: ast.AST) -> list[ast.Call]:
    """Collect every Call node inside the given AST subtree."""
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)]


def _is_agent_memory_add(call: ast.Call) -> bool:
    """Match ``<anything>.memory.add(...)`` — attribute chain check."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "add":
        return False
    inner = func.value
    if not (isinstance(inner, ast.Attribute) and inner.attr == "memory"):
        return False
    return True


def _is_msg_with_prompt(call_arg: ast.AST) -> bool:
    """Match ``Msg("user", prompt, "user")`` — the duplicate offender."""
    if not isinstance(call_arg, ast.Call):
        return False
    if not (isinstance(call_arg.func, ast.Name) and call_arg.func.id == "Msg"):
        return False
    # Walk args looking for ``Name(prompt)`` — both keyword and positional.
    for a in list(call_arg.args) + [kw.value for kw in call_arg.keywords]:
        if isinstance(a, ast.Name) and a.id == "prompt":
            return True
    return False


def _is_msg_with_synthesis_hint(call_arg: ast.AST) -> bool:
    """Match the legit ``Msg("user", synthesis_hint, "user")`` in fallback."""
    if not isinstance(call_arg, ast.Call):
        return False
    if not (isinstance(call_arg.func, ast.Name) and call_arg.func.id == "Msg"):
        return False
    for a in list(call_arg.args) + [kw.value for kw in call_arg.keywords]:
        if isinstance(a, ast.Name) and a.id == "synthesis_hint":
            return True
    return False


def _calls_stream_with_session_messages(node: ast.AST) -> bool:
    """True iff the function body calls ``streaming_agent.stream(session_messages=...)``."""
    for call in _calls_in(node):
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "stream":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "streaming_agent"):
            continue
        for kw in call.keywords:
            if kw.arg == "session_messages":
                return True
    return False


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_run_item_workflow_no_pre_add() -> None:
    tree = ast.parse(_load_source())
    fn = _find_function(tree, "_run_item_via_workflow")
    for call in _calls_in(fn):
        if not _is_agent_memory_add(call):
            continue
        # Found `<...>.memory.add(<arg>)` — ensure the arg is NOT
        # `Msg("user", prompt, ...)`.
        for arg in call.args:
            assert not _is_msg_with_prompt(arg), (
                "regression: _run_item_via_workflow re-introduced "
                "agent.memory.add(Msg(\"user\", prompt, ...)). "
                "StreamingAgent.stream() already passes the prompt to "
                "agent.reply(user_msg), which adds it to memory once. "
                "Adding it manually causes a duplicate user message."
            )


def test_run_item_workflow_still_streams() -> None:
    """Sanity pin: removing the pre-add must not have removed the stream call."""
    tree = ast.parse(_load_source())
    fn = _find_function(tree, "_run_item_via_workflow")
    assert _calls_stream_with_session_messages(fn), (
        "_run_item_via_workflow no longer calls streaming_agent.stream("
        "session_messages=...) — the fix removed too much"
    )


def test_fallback_recover_preserved() -> None:
    """The legit memory.add in `_fallback_recover_final_text` must stay.

    There, `agent.reply()` is called WITHOUT user_msg, so reply does NOT
    add anything — the manual add of `synthesis_hint` is the only way
    the model sees the synthesis instruction.
    """
    tree = ast.parse(_load_source())
    fn = _find_function(tree, "_fallback_recover_final_text")
    has_synthesis_add = False
    for call in _calls_in(fn):
        if not _is_agent_memory_add(call):
            continue
        for arg in call.args:
            if _is_msg_with_synthesis_hint(arg):
                has_synthesis_add = True
                break
    assert has_synthesis_add, (
        "regression: _fallback_recover_final_text lost its "
        "agent.memory.add(Msg(\"user\", synthesis_hint, \"user\")) — "
        "that one is necessary because agent.reply() is called with no "
        "user_msg, so reply won't add the synthesis hint by itself."
    )


def test_msg_import_inside_workflow_helper_dropped() -> None:
    """After the fix, ``_run_item_via_workflow`` should no longer import
    ``Msg`` locally — the only thing that needed it was the removed
    duplicate add. Keeping a dead inline import would leave a lint smell
    and confuse future readers about whether Msg is used here."""
    tree = ast.parse(_load_source())
    fn = _find_function(tree, "_run_item_via_workflow")
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == "agentscope.message":
            for alias in node.names:
                assert alias.name != "Msg", (
                    "regression: _run_item_via_workflow has a dead "
                    "`from agentscope.message import Msg` line — the "
                    "function body no longer uses Msg after the dedup fix"
                )


def test_fallback_recover_still_imports_msg() -> None:
    """Inverse pin: the fallback helper DOES still need Msg."""
    tree = ast.parse(_load_source())
    fn = _find_function(tree, "_fallback_recover_final_text")
    has_msg_import = False
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == "agentscope.message":
            for alias in node.names:
                if alias.name == "Msg":
                    has_msg_import = True
    assert has_msg_import, (
        "_fallback_recover_final_text must keep its local Msg import — "
        "it constructs Msg(\"user\", synthesis_hint, \"user\") right before "
        "agent.reply()"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_all() -> int:
    cases = [
        ("_run_item_via_workflow no pre-add of Msg(\"user\", prompt, ...)",
         test_run_item_workflow_no_pre_add),
        ("_run_item_via_workflow still calls streaming_agent.stream(session_messages=...)",
         test_run_item_workflow_still_streams),
        ("_fallback_recover_final_text keeps its agent.memory.add(synthesis_hint)",
         test_fallback_recover_preserved),
        ("_run_item_via_workflow no longer imports Msg locally",
         test_msg_import_inside_workflow_helper_dropped),
        ("_fallback_recover_final_text still imports Msg locally",
         test_fallback_recover_still_imports_msg),
    ]
    print("=== batch_user_msg_dedup_selftest ===")
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
        print(f"=== batch_user_msg_dedup_selftest: FAILED ({failed}/{len(cases)}) ===")
        return 1
    print("=== batch_user_msg_dedup_selftest: OK ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())

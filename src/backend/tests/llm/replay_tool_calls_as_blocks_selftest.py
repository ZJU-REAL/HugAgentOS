"""Selftest for ``replay_tool_calls_as_blocks`` + ``build_replay_dicts``.

Pins the structured replay path that replaced the prose
``[tool_call#N name status=ok]`` history flattening — see the
chat_mpmd5u5w_2ntpls78 incident where after two turns of prose-flattened
tool_calls the model started emitting tool-call XML as plain text content
instead of structured tool_use blocks.

Run with:
    ``PYTHONPATH=src/backend python -m tests.replay_tool_calls_as_blocks_selftest``
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import ModuleType


def _load_message_compat() -> ModuleType:
    """Load message_compat without triggering core.llm.__init__ (agentscope-heavy)."""
    if "agentscope" not in sys.modules:
        ag = ModuleType("agentscope")
        sys.modules["agentscope"] = ag
    if "agentscope.memory" not in sys.modules:
        mem = ModuleType("agentscope.memory")
        mem.InMemoryMemory = object  # type: ignore[attr-defined]
        sys.modules["agentscope.memory"] = mem
    if "agentscope.message" not in sys.modules:
        msgmod = ModuleType("agentscope.message")

        class _Msg:
            def __init__(self, name=None, content="", role="user"):
                self.name = name
                self.content = content
                self.role = role

            def get_text_content(self):
                return self.content if isinstance(self.content, str) else ""

        def _text_block(type="text", text=""):
            return {"type": type, "text": text}

        msgmod.Msg = _Msg  # type: ignore[attr-defined]
        msgmod.TextBlock = _text_block  # type: ignore[attr-defined]
        sys.modules["agentscope.message"] = msgmod

    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.normpath(os.path.join(here, "..", "..", "core", "llm", "message_compat.py"))
    spec = importlib.util.spec_from_file_location("_mc_under_test", target)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mc = _load_message_compat()
replay_tool_calls_as_blocks = _mc.replay_tool_calls_as_blocks
build_replay_dicts = _mc.build_replay_dicts
dict_to_msg = _mc.dict_to_msg


def test_empty_input_returns_empty_lists():
    use, res = replay_tool_calls_as_blocks(None)
    assert use == [] and res == []
    use, res = replay_tool_calls_as_blocks([])
    assert use == [] and res == []
    use, res = replay_tool_calls_as_blocks("not a list")  # type: ignore[arg-type]
    assert use == [] and res == []
    print("✓ 空/非法输入 → 双空列表")


def test_pairs_have_matching_id_and_name():
    calls = [
        {"tool_id": "tc_001", "tool_name": "Read", "tool_args": {"file_path": "/a"},
         "result": {"ok": True}, "status": "success"},
        {"tool_id": "tc_002", "tool_name": "Write", "tool_args": {"file_path": "/b"},
         "result": {"ok": True, "file_id": "fid_xyz"}, "status": "success"},
    ]
    use, res = replay_tool_calls_as_blocks(calls)
    assert len(use) == 2 and len(res) == 2
    for i, (u, r) in enumerate(zip(use, res)):
        assert u["type"] == "tool_call"  # AS2.0: tool_use → tool_call
        assert r["type"] == "tool_result"
        assert u["id"] == r["id"], f"call#{i} id mismatch: {u['id']} vs {r['id']}"
        assert u["name"] == r["name"]
    assert use[0]["id"] == "tc_001" and use[1]["id"] == "tc_002"
    print("✓ tool_call 与 tool_result 通过 id/name 严格配对")


def test_synthetic_id_when_missing():
    calls = [
        {"tool_name": "Read", "tool_args": {}, "result": {}, "status": "success"},
        {"tool_name": "Write", "tool_args": {}, "result": {}, "status": "success"},
    ]
    use, res = replay_tool_calls_as_blocks(calls)
    assert use[0]["id"] == "hist_1" and use[1]["id"] == "hist_2"
    assert use[0]["id"] == res[0]["id"]
    print("✓ DB 行缺 tool_id 时回退到稳定的 hist_N 合成 id")


def test_structured_args_survive_intact():
    calls = [
        {
            "tool_id": "t1",
            "tool_name": "Write",
            "tool_args": {"file_path": "/myspace/a.docx", "content": "tiny"},
            "result": {"ok": False, "error": "must end with .xlsx"},
            "status": "success",
        }
    ]
    use, res = replay_tool_calls_as_blocks(calls)
    # AS2.0: input is a JSON string (not a dict)
    args = json.loads(use[0]["input"])
    assert args["file_path"] == "/myspace/a.docx"
    assert args["content"] == "tiny"
    # Result is serialized to a string in ToolResultBlock.output
    assert "must end with .xlsx" in res[0]["output"]
    assert '"ok": false' in res[0]["output"]
    print("✓ 短 args 保持 dict 结构，result 串行化但语义保留")


def test_long_strings_truncated_with_default_budget():
    long_md = "x" * 8000
    huge = "L" * 50_000
    calls = [
        {
            "tool_id": "t1",
            "tool_name": "Write",
            "tool_args": {"file_path": "/myspace/b.docx", "content": long_md},
            "result": {"ok": True, "type": "create", "file_id": "deadbeef"},
            "status": "success",
        },
        {
            "tool_id": "t2",
            "tool_name": "read_artifact",
            "tool_args": {"file_id": "abc"},
            "result": {"type": "text", "content": huge, "end_line": 213},
            "status": "success",
        },
    ]
    use, res = replay_tool_calls_as_blocks(calls)
    # 500 char default budget for args (input is a JSON string, decode back to dict to assert)
    args0 = json.loads(use[0]["input"])
    assert "truncated" in args0["content"]
    assert "total 8000 chars" in args0["content"]
    # 1000 char default budget for result
    assert "truncated" in res[1]["output"]
    assert "total 50000 chars" in res[1]["output"]
    # Structural fields survive
    assert "deadbeef" in res[0]["output"]
    assert "end_line" in res[1]["output"]
    print("✓ 默认 500/1000 budget 截断长字符串，结构字段原样保留")


def test_failed_status_preserved_in_output():
    calls = [
        {"tool_id": "t1", "tool_name": "bash", "tool_args": {"cmd": "..."},
         "result": {"stderr": "oops"}, "status": "error"},
    ]
    _use, res = replay_tool_calls_as_blocks(calls)
    assert res[0]["output"].startswith("[status=error]"), \
        f"failure signal must lead output, got: {res[0]['output'][:60]!r}"
    assert "oops" in res[0]["output"]
    print("✓ 失败 tool_call 在 output 前注入 [status=…] 标记")


def test_string_args_wrapped_as_dict():
    """Some legacy rows store args as a plain string rather than dict."""
    calls = [{"tool_id": "t1", "tool_name": "Read", "tool_args": "some string",
              "result": {"ok": True}, "status": "success"}]
    use, _ = replay_tool_calls_as_blocks(calls)
    args = json.loads(use[0]["input"])
    assert isinstance(args, dict)
    assert args["value"] == "some string"
    print("✓ 非 dict args 被包成 {value: …} 保持 input 的 dict 语义不变量")


def test_string_result_kept_as_string():
    calls = [{"tool_id": "t1", "tool_name": "bash", "tool_args": {},
              "result": "stdout payload", "status": "success"}]
    _use, res = replay_tool_calls_as_blocks(calls)
    assert res[0]["output"] == "stdout payload"
    print("✓ result 已经是 string 时直接透传，不二次 json.dumps")


def test_malformed_entry_skipped():
    calls = [
        "not a dict",  # invalid
        {"tool_name": "Read", "tool_args": {}, "result": {}, "status": "success"},
    ]
    use, res = replay_tool_calls_as_blocks(calls)
    assert len(use) == 1 and len(res) == 1
    assert use[0]["name"] == "Read"
    print("✓ 非 dict 条目被跳过，不抛异常")


# ── build_replay_dicts: integration shape used by _load_session_messages ──

def test_build_replay_dicts_no_tool_calls_single_msg():
    out = build_replay_dicts("assistant", "just text", None)
    assert out == [{"role": "assistant", "content": "just text"}]

    out = build_replay_dicts("assistant", "just text", [])
    assert out == [{"role": "assistant", "content": "just text"}]
    print("✓ assistant 无 tool_calls → 单条 str-content dict")


def test_build_replay_dicts_with_tool_calls_yields_pair():
    calls = [
        {"tool_id": "t1", "tool_name": "Read", "tool_args": {"file_path": "/a"},
         "result": {"ok": True}, "status": "success"},
    ]
    out = build_replay_dicts("assistant", "thinking text", calls)
    assert len(out) == 2, "must emit assistant + tool carrier"
    assert out[0]["role"] == "assistant"
    assert out[1]["role"] == "tool", "carrier role MUST be 'tool' (not 'user')"

    # Assistant content: text block + tool_call block
    assert isinstance(out[0]["content"], list)
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["text", "tool_call"]
    assert out[0]["content"][0]["text"] == "thinking text"

    # Carrier content: list of tool_result blocks
    assert isinstance(out[1]["content"], list)
    assert all(b["type"] == "tool_result" for b in out[1]["content"])
    assert out[1]["content"][0]["id"] == out[0]["content"][1]["id"]
    print("✓ 带 tool_calls → 双 dict 包对，role='tool' 标记 carrier")


def test_build_replay_dicts_strips_empty_text():
    calls = [
        {"tool_id": "t1", "tool_name": "Read", "tool_args": {}, "result": {}, "status": "success"},
    ]
    out = build_replay_dicts("assistant", "   ", calls)
    # Pure-whitespace text should not produce a TextBlock
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["tool_call"], f"expected only tool_call, got {types}"
    print("✓ 全空白 content 不会插入空 TextBlock")


def test_build_replay_dicts_strips_thinking():
    """The tool_calls replay path does not go through dict_to_msg's str branch, so it must strip
    thinking itself—otherwise the previous round's reasoning monologue leaks into the next round's context
    (the chat_20260713_172036 incident: 30 segments of </think> narration were replayed, and the model
    imitated the narration, hallucinating tool execution in plain text)."""
    calls = [
        {"tool_id": "t1", "tool_name": "Read", "tool_args": {}, "result": {}, "status": "success"},
    ]
    raw = "Let me check the files</think>好的，我先查看文件。middle thinking</think>最终答案"
    out = build_replay_dicts("assistant", raw, calls)
    text_blocks = [b for b in out[0]["content"] if b["type"] == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"] == "最终答案", \
        f"thinking must be stripped on tool_calls replay path, got: {text_blocks[0]['text']!r}"

    # Stripping to fully empty → no empty TextBlock produced
    out2 = build_replay_dicts("assistant", "all thinking</think>", calls)
    types2 = [b["type"] for b in out2[0]["content"]]
    assert types2 == ["tool_call"]
    print("✓ 带 tool_calls 回放路径剥离 </think> 泄漏（含剥空不留空块）")


def test_build_replay_dicts_user_passthrough():
    out = build_replay_dicts("user", "hi", None)
    assert out == [{"role": "user", "content": "hi"}]
    # Even if a user row somehow had tool_calls metadata, we don't replay it
    out = build_replay_dicts("user", "hi", [{"tool_name": "Read"}])
    assert out == [{"role": "user", "content": "hi"}]
    print("✓ 非 assistant role 走 passthrough，不触发回放")


def test_dict_to_msg_tool_role_maps_to_assistant():
    # AS2.0: tool_result blocks can only attach to assistant (1.x mapped them to user)
    msg = dict_to_msg({"role": "tool", "content": [{"type": "tool_result", "id": "t1",
                                                     "name": "Read", "output": "ok"}]})
    assert msg.role == "assistant", "tool role must collapse to assistant for AS2.0 Msg"
    assert isinstance(msg.content, list)
    print("✓ dict_to_msg: role='tool' 映射到 Msg.role='assistant'，保留 block 内容")


def main() -> None:
    test_empty_input_returns_empty_lists()
    test_pairs_have_matching_id_and_name()
    test_synthetic_id_when_missing()
    test_structured_args_survive_intact()
    test_long_strings_truncated_with_default_budget()
    test_failed_status_preserved_in_output()
    test_string_args_wrapped_as_dict()
    test_string_result_kept_as_string()
    test_malformed_entry_skipped()
    test_build_replay_dicts_no_tool_calls_single_msg()
    test_build_replay_dicts_with_tool_calls_yields_pair()
    test_build_replay_dicts_strips_empty_text()
    test_build_replay_dicts_strips_thinking()
    test_build_replay_dicts_user_passthrough()
    test_dict_to_msg_tool_role_maps_to_assistant()
    print("\n=== replay_tool_calls_as_blocks_selftest: OK ===")


if __name__ == "__main__":
    main()

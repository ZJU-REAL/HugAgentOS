"""Edit/Write trust the model like pi's coding agent: no Read precondition, lenient matching."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.llm.factory.tools.definitions import ToolAssembly
from core.llm.tools import register_edit, register_read, register_write
from core.llm.tools.text_match import EditMatchError, apply_edit

PATH = "/workspace/demo.py"
SMALL = "".join(f"line {i}\n" for i in range(1, 51)).encode()
BIG = "".join(f"line {i}\n" for i in range(1, 2501)).encode()


class _Sandbox:
    name = "fake"

    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def get_file(self, session, path, user_id=None):
        from core.sandbox import SandboxError

        if path not in self.files:
            raise SandboxError("not found")
        await asyncio.sleep(0)  # let a concurrent call interleave if nothing serializes them
        return self.files[path]

    async def put_file(self, session, path, data, user_id=None):
        self.files[path] = data


@pytest.fixture
def tools(monkeypatch):
    sandbox = _Sandbox()
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: sandbox)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)

    async def _bash(*args, **kwargs):
        return 0, "", ""

    monkeypatch.setattr("core.llm.tools.write_tool.sandbox_exec_bash", _bash)
    collector = ToolAssembly()
    common = dict(chat_id="c1", user_id="u1")
    register_read(collector, **common)
    register_edit(collector, **common)
    register_write(collector, **common)
    funcs = {tool.name: tool._func for tool in collector}

    async def call(name, **kwargs):
        block = (await funcs[name](**kwargs)).content[0]
        return json.loads(block["text"] if isinstance(block, dict) else block.text)

    return sandbox, call


async def test_edit_without_any_read(tools):
    sandbox, call = tools
    sandbox.files[PATH] = SMALL
    result = await call("Edit", file_path=PATH, old_string="line 12\n", new_string="LINE 12\n")
    assert result["ok"] is True and result["fuzzy_match"] is False
    assert b"LINE 12\n" in sandbox.files[PATH]


async def test_edit_after_partial_read_of_a_large_file(tools):
    sandbox, call = tools
    sandbox.files[PATH] = BIG
    read = await call("Read", file_path=PATH, offset=2400, limit=10)
    assert read["start_line"] == 2400
    result = await call("Edit", file_path=PATH, old_string="line 2405\n", new_string="X\n")
    assert result["ok"] is True
    assert sandbox.files[PATH].count(b"\n") == 2500


async def test_write_overwrites_an_unread_existing_file(tools):
    sandbox, call = tools
    sandbox.files[PATH] = b"old\n"
    result = await call("Write", file_path=PATH, content="new\n")
    assert result["ok"] is True and result["type"] == "update"
    assert "-old" in result["diff"] and "+new" in result["diff"]
    assert sandbox.files[PATH] == b"new\n"


async def test_edit_still_rejects_missing_and_ambiguous_text(tools):
    sandbox, call = tools
    sandbox.files[PATH] = b"a = 1\na = 1\n"
    missing = await call("Edit", file_path=PATH, old_string="b = 2", new_string="b = 3")
    assert "找不到" in missing["error"]
    ambiguous = await call("Edit", file_path=PATH, old_string="a = 1", new_string="a = 2")
    assert "出现 2 次" in ambiguous["error"]
    assert sandbox.files[PATH] == b"a = 1\na = 1\n"


async def test_concurrent_edits_on_one_file_are_serialized(tools):
    sandbox, call = tools
    sandbox.files[PATH] = SMALL
    first, second = await asyncio.gather(
        call("Edit", file_path=PATH, old_string="line 3\n", new_string="THREE\n"),
        call("Edit", file_path=PATH, old_string="line 7\n", new_string="SEVEN\n"),
    )
    assert first["ok"] and second["ok"]
    assert b"THREE\n" in sandbox.files[PATH] and b"SEVEN\n" in sandbox.files[PATH]


def test_fuzzy_match_rewrites_only_touched_lines():
    raw = "keep  \nprint(“hi”)   \ntail\t\n"
    result = apply_edit(raw, 'print("hi")', 'print("bye")')
    assert result.fuzzy is True
    assert result.new_raw == 'keep  \nprint("bye")\ntail\t\n'


def test_crlf_and_bom_are_preserved():
    raw = "﻿one\r\ntwo\r\nthree\r\n"
    result = apply_edit(raw, "two\n", "2\n")
    assert result.new_raw == "﻿one\r\n2\r\nthree\r\n"


def test_replace_all_and_no_op_edits():
    assert apply_edit("x x x", "x", "y", replace_all=True).new_raw == "y y y"
    with pytest.raises(EditMatchError):
        apply_edit("a’b", "a'b", "a’b")

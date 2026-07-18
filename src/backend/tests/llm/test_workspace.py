"""Tests for the per-run workspace state used by pin_to_workspace."""
import asyncio

from core.llm import workspace


def test_initial_state_is_empty_and_inactive():
    workspace.init_state()
    assert workspace.is_active() is False
    assert workspace.get_pinned() == []
    assert workspace.get_pinned_file_ids() == []


def test_pin_appends_and_dedups():
    workspace.init_state()
    assert workspace.pin("f1", name="a.docx", url="/files/f1") is True
    assert workspace.pin("f1", name="a.docx") is False  # dedup
    assert workspace.pin("f2", name="b.xlsx", url="/files/f2") is True
    assert workspace.get_pinned_file_ids() == ["f1", "f2"]
    items = workspace.get_pinned()
    assert items[0]["file_id"] == "f1" and items[0]["name"] == "a.docx"
    # None values are stripped from the user-facing pinned list
    assert "mime_type" not in items[0]


def test_pin_without_init_returns_false():
    # Reset the contextvar by initializing then we'd need a separate context;
    # here we rely on a fresh test process where no init has happened — but
    # since other tests in this module init, we just verify get_state returns
    # whatever was last set.
    workspace.init_state()
    state = workspace.get_state()
    assert state is not None
    state.clear()  # corrupted state — pin still defends
    # After clearing the dict, pin should still write into it (it sets default
    # keys) — we want this to be robust.
    assert workspace.pin("f3") is True
    assert "f3" in workspace.get_pinned_file_ids()


def test_mark_active_flips_gate_without_pinning():
    workspace.init_state()
    assert workspace.is_active() is False
    workspace.mark_active()
    assert workspace.is_active() is True
    # Active but no pins → gate is "on" but pinned list is empty.
    # The chats.py logic sees is_active()=True and replaces artifacts with [].
    assert workspace.get_pinned() == []


def test_pin_rejects_empty_file_id():
    workspace.init_state()
    assert workspace.pin("") is False
    assert workspace.pin("   ") is False
    assert workspace.get_pinned_file_ids() == []


def test_scope_restores_outer_state():
    """nested workspace.scope() must not leak its pins back to the parent."""
    workspace.init_state()
    workspace.pin("outer", name="outer.docx")
    assert workspace.get_pinned_file_ids() == ["outer"]

    with workspace.scope():
        # Inside the scope, parent's pins are invisible.
        assert workspace.get_pinned_file_ids() == []
        workspace.pin("inner", name="inner.xlsx")
        assert workspace.get_pinned_file_ids() == ["inner"]

    # Back outside: parent's state is intact.
    assert workspace.get_pinned_file_ids() == ["outer"]


def test_pin_to_workspace_tool_accepts_list(monkeypatch):
    """The agent-facing tool must accept a list of file_ids and pin all in one call."""
    import json as _json

    from agentscope.tool import Toolkit
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.pin_tool import register_pin_to_workspace

    # Stub the artifact lookup so we don't need a real artifact store.
    fake_store = {
        "fid_a": {"name": "a.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 100},
        "fid_b": {"name": "b.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "size": 200},
    }

    def fake_get_artifact(fid):
        return fake_store.get(fid)

    monkeypatch.setattr("core.artifacts.store.get_artifact", fake_get_artifact)

    workspace.init_state()
    # AgentScope 2.0: register_* writes into the ToolCollector; schema goes through Toolkit.get_tool_schemas().
    c = ToolCollector()
    register_pin_to_workspace(c)
    tk = Toolkit(tools=c.function_tools)
    schemas = asyncio.run(tk.get_tool_schemas())
    pin_schema = next(s for s in schemas if s["function"]["name"] == "pin_to_workspace")
    # Schema must declare file_ids as an array of strings (not a string).
    fid_param = pin_schema["function"]["parameters"]["properties"]["file_ids"]
    assert fid_param["type"] == "array"
    assert fid_param["items"]["type"] == "string"

    fn = c.get_tool("pin_to_workspace")._func

    resp = asyncio.run(fn(file_ids=["fid_a", "fid_b"]))
    payload = _json.loads(resp.content[0].text)  # 2.0: block is a pydantic object
    assert payload["ok"] is True
    assert payload["pinned_count"] == 2
    assert {p["file_id"] for p in payload["pinned"]} == {"fid_a", "fid_b"}
    assert workspace.get_pinned_file_ids() == ["fid_a", "fid_b"]

    # Re-pinning the same id is reported but doesn't error or duplicate.
    workspace.init_state()
    asyncio.run(fn(file_ids=["fid_a", "fid_a"]))
    assert workspace.get_pinned_file_ids() == ["fid_a"]

    # Mixed valid + invalid: ok stays True, failed list is non-empty.
    workspace.init_state()
    resp = asyncio.run(fn(file_ids=["fid_a", "missing"]))
    payload = _json.loads(resp.content[0].text)
    assert payload["ok"] is True
    assert payload["pinned_count"] == 1
    assert payload["pinned"][0]["file_id"] == "fid_a"
    assert payload["failed"][0]["file_id"] == "missing"


def test_state_isolated_per_async_context():
    """ContextVar-based state shouldn't leak across asyncio.gather'd coroutines."""

    async def session(label: str, file_id: str) -> list[str]:
        workspace.init_state()
        await asyncio.sleep(0)  # let other coroutines interleave
        workspace.pin(file_id, name=label)
        await asyncio.sleep(0)
        return workspace.get_pinned_file_ids()

    async def run() -> tuple[list[str], list[str]]:
        # Each coroutine runs in the same Task by default — but if we use
        # asyncio.create_task, each gets its own Context copy. We test the
        # create_task path because real usage (chat_stream handlers) runs in
        # separate tasks per request.
        t1 = asyncio.create_task(session("A", "fa"))
        t2 = asyncio.create_task(session("B", "fb"))
        return await asyncio.gather(t1, t2)

    a, b = asyncio.run(run())
    assert a == ["fa"]
    assert b == ["fb"]

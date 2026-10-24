"""Adversarial lifecycle, pagination, authority and schema tests."""
import asyncio
import sys
from types import SimpleNamespace
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from core.llm.mcp.lifetime import discover
from core.llm.mcp import chat_sessions
from tests.llm.test_mcp_parallel_execution import probe_server
from tests.llm.test_mcp_native_capabilities import call_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cursor", [False, True])
async def test_paginated_discovery_and_repeated_cursor_rejection(repeat_cursor):
    calls = []
    async def listing(ctx, params):
        cursor = params.cursor if params else None
        calls.append(cursor)
        return types.ListToolsResult(tools=[types.Tool(name="one" if cursor is None else "two", input_schema={"type":"object"})],
                                     next_cursor="next" if cursor is None or repeat_cursor else None)
    low = Server("paged", on_list_tools=listing)
    server = MCPServer("paged")
    server._lowlevel_server = low
    async with probe_server(server) as (url, _):
        if repeat_cursor:
            with pytest.raises(BaseExceptionGroup) as failure:
                await discover("paged", {"transport":"streamable_http", "url":url})
            assert failure.value.subgroup(lambda e: isinstance(e, ValueError) and "pagination cursor repeated" in str(e)) is not None
        else:
            resource = await discover("paged", {"transport":"streamable_http", "url":url})
            try:
                assert [t.name for t in resource.tools] == ["one", "two"]
                assert calls == [None, "next"]
            finally:
                await resource.close()


@pytest.mark.asyncio
async def test_resource_only_server_never_receives_tools_list():
    async def resources(ctx, params):
        return types.ListResourcesResult(resources=[types.Resource(name="only",uri="doc://only")])
    async def templates(ctx, params):
        return types.ListResourceTemplatesResult(resource_templates=[])
    async def read(ctx, params):
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=params.uri, text="only-resource")])
    server = MCPServer("resource-only")
    server._lowlevel_server = Server("resource-only", on_list_resources=resources,
                                     on_list_resource_templates=templates, on_read_resource=read)
    async with probe_server(server) as (url, _):
        resource = await discover("only", {"transport":"streamable_http","url":url})
        try:
            assert len(resource.tools) == 1
            assert "only-resource" in str(await resource.tools[0]())
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_schema_change_during_header_recovery_never_replays_call():
    calls = []
    async def listing(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="write", input_schema={"type":"object",
            "properties": {"new": {"type":"string"}} if calls else {}})])
    async def invoke(ctx, params):
        calls.append(params.name)
        raise MCPError(code=-32020, message="schema changed")
    server = MCPServer("changes")
    server._lowlevel_server = Server("changes", on_list_tools=listing, on_call_tool=invoke)
    async with probe_server(server) as (url, _):
        resource = await discover("changes", {"transport":"streamable_http","url":url})
        try:
            with pytest.raises(Exception):
                await resource.tools[0]()
            assert calls == ["write"]
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_chat_deleted_while_first_process_is_opening(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text('import time\ntime.sleep(300)\n')
    cfg = {"transport":"stdio","command":sys.executable,"args":[str(script)]}
    opening = asyncio.create_task(discover("slow",cfg,scope=("user","deleted","")))
    await asyncio.sleep(.15)
    await chat_sessions.close_chat("deleted")
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await asyncio.wait_for(opening, 5)
    assert not any(k[1] == "deleted" for k in chat_sessions._entries)
    assert not any(t.get_name() == "mcp-session:slow" and not t.done() for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_sampling_budget_is_reserved_before_slow_authorization():
    call = call_scope()
    ready = asyncio.Event()
    async def validate():
        await ready.wait()
    call.validate = validate
    params = types.CreateMessageRequestParams(messages=[], max_tokens=10)
    requests = [asyncio.create_task(call.sampling(None, params)) for _ in range(12)]
    await asyncio.sleep(0)
    assert call.run.samples == 4
    ready.set()
    results = await asyncio.gather(*requests)
    assert sum(isinstance(r,types.CreateMessageResult) for r in results) == 4


@pytest.mark.asyncio
async def test_connector_revoked_while_user_is_answering(monkeypatch):
    call = call_scope()
    allowed = True
    call.run.agent._jx_prepared_capabilities = SimpleNamespace(validate=lambda **_:None,
        validate_tool_call=lambda _:allowed)
    async def answer(**kwargs):
        nonlocal allowed
        allowed = False
        return {"status":"answered","answers":[{"id":"name","custom":"Ada"}]}
    monkeypatch.setattr("core.llm.tools.user_questions.ask",answer)
    params = types.ElicitRequestFormParams(message="Name",requested_schema={"type":"object",
        "properties":{"name":{"type":"string"}},"required":["name"]})
    with pytest.raises(PermissionError):
        await call.elicitation(None,params)


@pytest.mark.asyncio
async def test_outer_cancellation_cancels_pending_model_work():
    call = call_scope()
    stopped = asyncio.Event()
    class SlowModel:
        model = "slow"
        async def __call__(self,*args,**kwargs):
            try:
                await asyncio.sleep(300)
            finally:
                stopped.set()
    call.run.agent.model = SlowModel()
    params = types.CreateMessageRequestParams(messages=[],max_tokens=10)
    task = asyncio.create_task(call.dispatch("sampling",None,params))
    await asyncio.sleep(.01)
    await call.finish()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and not call.pending


@pytest.mark.asyncio
async def test_deleted_chat_revokes_every_run_sharing_its_session(tmp_path):
    script = tmp_path / "shared.py"
    script.write_text('from mcp.server.mcpserver import MCPServer\ns=MCPServer("shared")\n@s.tool()\ndef echo(text: str)->str: return text\ns.run()\n')
    cfg = {"transport":"stdio", "command":sys.executable, "args":[str(script)]}
    first = await discover("shared", cfg, scope=("user", "deleted-shared", ""))
    second = await asyncio.wait_for(discover("shared", cfg, scope=("user", "deleted-shared", "")), 5)
    assert first.session is second.session
    await chat_sessions.close_chat("deleted-shared")
    for run in (first, second):
        with pytest.raises(RuntimeError, match="lease is closed"):
            await run.tools[0](text="after delete")
    assert first.session._owner.done()
    await first.close()
    await second.close()
    assert not any(k[1] == "deleted-shared" for k in chat_sessions._entries)


@pytest.mark.asyncio
async def test_discovery_repeated_cancel_waits_for_owner_exit(monkeypatch):
    from core.llm.mcp import session as session_module
    entered, exiting, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            exiting.set()
            await finish.wait()
        @property
        def server_capabilities(self):
            return SimpleNamespace(tools=True)
        async def list_tools(self, **kwargs):
            entered.set()
            await asyncio.sleep(300)
    monkeypatch.setattr(session_module, "make_client", lambda *a, **kw:Client())
    opening = asyncio.create_task(discover("repeat-cancel", {"transport":"streamable_http"}))
    await entered.wait()
    opening.cancel()
    await exiting.wait()
    opening.cancel()
    await asyncio.sleep(.01)
    assert not opening.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert not any(t.get_name() == "mcp-session:repeat-cancel" and not t.done() for t in asyncio.all_tasks())


def test_retired_column_migration_preserves_existing_servers():
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[2] / "alembic/versions/mcpnative01_drop_stable.py"
    spec = importlib.util.spec_from_file_location("mcp_migration",path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE admin_mcp_servers (server_id TEXT PRIMARY KEY, is_stable BOOLEAN NOT NULL)")
        connection.exec_driver_sql("INSERT INTO admin_mcp_servers VALUES ('keep', 0)")
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert [c["name"] for c in sa.inspect(connection).get_columns("admin_mcp_servers")] == ["server_id"]
            assert connection.exec_driver_sql("SELECT server_id FROM admin_mcp_servers").scalar() == "keep"
            module.downgrade()
            assert connection.exec_driver_sql("SELECT is_stable FROM admin_mcp_servers").scalar() == 1
            module.upgrade()

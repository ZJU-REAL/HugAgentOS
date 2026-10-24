"""Migration seams outside the main factory: LiteLLM and datasource probes."""
import os
import sys
from types import SimpleNamespace

import pytest
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from openai.types.chat import ChatCompletionChunk
from mcp.server.mcpserver import MCPServer

from core.models.providers.vendor_models import LiteLLMChatModel
from core.llm.mcp import lifetime
from features.datasources import introspection
from tests.llm.test_mcp_parallel_execution import probe_server


@pytest.mark.asyncio
async def test_litellm_stream_uses_native_parser(monkeypatch):
    calls = []
    closed = []

    async def completion(**kwargs):
        calls.append(kwargs)

        async def stream():
            for delta, finish in [({"role": "assistant", "content": "vendor-ok"}, None), ({}, "stop")]:
                yield ChatCompletionChunk.model_validate({
                    "id": "litellm-stream", "object": "chat.completion.chunk",
                    "created": 1, "model": "vendor-probe",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                })
        class VendorStream:
            def __aiter__(self):
                return stream()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                closed.append(True)

        return VendorStream()

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    model = LiteLLMChatModel(
        litellm_model="bedrock/vendor-probe", litellm_kwargs={}, model="vendor-probe",
        parameters=OpenAIChatModel.Parameters(), stream=True, timeout=5, context_size=32768,
    )
    response = await model._call_api("vendor-probe", [UserMsg(name="user", content="hello")])
    chunks = [chunk async for chunk in response]
    assert calls[0]["stream"] is True
    assert calls[0]["model"] == "bedrock/vendor-probe"
    assert any("vendor-ok" in str(chunk.content) for chunk in chunks)
    assert chunks[-1].finished_reason.value == "completed"
    assert closed == [True]


TOOLS = '''
@s.tool()
def search_objects(source: str = "", database: str = "") -> str:
    return '{"tables":[{"schema":"public","table":"orders","columns":[{"name":"id","type":"integer"}]}]}'
@s.tool()
def list_indices(index_pattern: str) -> str:
    return '["orders"]'
@s.tool()
def get_mappings(index: str) -> str:
    return '{"orders":{"mappings":{"properties":{"id":{"type":"integer"}}}}}'
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
@pytest.mark.parametrize("kind", ["sql", "es"])
async def test_real_datasource_probe_releases_resources(monkeypatch, tmp_path, transport, kind):
    resources = []
    original = lifetime.discover

    async def record_discovery(*args, **kwargs):
        resource = await original(*args, **kwargs)
        resources.append(resource)
        return resource

    monkeypatch.setattr(lifetime, "discover", record_discovery)

    async def run(cfg):
        monkeypatch.setattr(introspection, "_get_mcp_cfg", lambda *_: (cfg, None))
        if kind == "sql":
            rows, error = await introspection._introspect_sql("source", "database")
        else:
            rows, error = await introspection._introspect_es("source", "orders*")
        assert error is None
        assert rows[0]["table"] == "orders"
        assert rows[0]["columns"] == [{"name": "id", "type": "integer"}]
        assert len(resources) == 1
        assert resources[0].session._owner.done()
        assert not resources[0].session._owner.cancelled()

    if transport == "streamable_http":
        server = MCPServer("datasource-probe")
        exec(TOOLS, {"s": server})
        async with probe_server(server) as (url, _):
            await run({"transport": transport, "url": url})
    else:
        pid_file = tmp_path / "pid"
        script = tmp_path / "server.py"
        script.write_text(
            "import os\nfrom pathlib import Path\nfrom mcp.server.mcpserver import MCPServer\n"
            + f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            + 's=MCPServer("datasource-probe")\n' + TOOLS + '\ns.run()\n'
        )
        await run({"transport": transport, "command": sys.executable, "args": [str(script)]})
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

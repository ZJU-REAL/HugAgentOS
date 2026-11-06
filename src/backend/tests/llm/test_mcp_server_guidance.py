"""MCP server purpose and handshake instructions reach the agent's system prompt."""

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from core.llm.mcp import server_guidance
from core.llm.mcp.lifetime import MCPResource, discover
from tests.llm.test_mcp_parallel_execution import probe_server


def _resource(name, tools, *, display_name=None, description="", instructions=""):
    resource = MCPResource(
        name,
        tools=[SimpleNamespace(name=t) for t in tools],
        session=SimpleNamespace(instructions=instructions),
        display_name=display_name,
    )
    resource.description = description
    return resource


@pytest.mark.asyncio
async def test_live_session_keeps_the_servers_handshake_instructions():
    server = MCPServer("papers", instructions="Use these tools for research-paper questions.")

    @server.tool(annotations={"readOnlyHint": True})
    def search_papers(query: str) -> str:
        return query

    async with probe_server(server) as (url, _):
        resource = await discover("papers", {"transport": "streamable_http", "url": url})
        try:
            assert resource.instructions == "Use these tools for research-paper questions."
            section = server_guidance.render_mcp_server_section([resource], {"search_papers"})
        finally:
            await resource.close()
    assert '<mcp_server_instructions server="papers">' in section
    assert "Use these tools for research-paper questions." in section
    assert "`search_papers`" in section


def test_section_lists_purpose_tools_and_instructions_sorted_by_server():
    section = server_guidance.render_mcp_server_section(
        [
            _resource("zeta", ["z_tool"], description="Z 服务"),
            _resource(
                "alpha",
                ["find", "read"],
                display_name="论文库",
                description="覆盖 arXiv 论文\n检索与全文",
                instructions="Use for academic questions.",
            ),
        ],
        {"find", "read", "z_tool"},
    )
    assert section.startswith("## 已接入的 MCP 服务")
    assert section.index("### 论文库（`alpha`）") < section.index("### zeta")
    assert "用途：覆盖 arXiv 论文 检索与全文" in section
    assert "工具：`find`、`read`" in section
    assert "Use for academic questions." in section
    # Byte-stable: the same servers in another order render identically.
    again = server_guidance.render_mcp_server_section(
        [
            _resource(
                "alpha",
                ["read", "find"],
                display_name="论文库",
                description="覆盖 arXiv 论文\n检索与全文",
                instructions="Use for academic questions.",
            ),
            _resource("zeta", ["z_tool"], description="Z 服务"),
        ],
        {"find", "read", "z_tool"},
    )
    assert again == section


def test_servers_without_a_visible_tool_are_not_described():
    section = server_guidance.render_mcp_server_section(
        [_resource("shadowed", ["clash"], instructions="never shown"), _resource("kept", ["ok"])],
        {"ok"},
    )
    assert "shadowed" not in section and "never shown" not in section
    assert "### kept" in section
    assert server_guidance.render_mcp_server_section([_resource("x", ["t"])], set()) == ""


def test_manifest_resource_without_session_has_no_instructions():
    resource = MCPResource("cloud", tools=[SimpleNamespace(name="t")])
    resource.description = "云端连接器"
    assert resource.instructions == ""
    section = server_guidance.render_mcp_server_section([resource], {"t"})
    assert "用途：云端连接器" in section and "mcp_server_instructions" not in section


def test_long_instructions_are_clipped_and_budget_drops_later_instructions():
    long_text = "x" * (server_guidance.MAX_INSTRUCTION_CHARS + 500)
    resources = [_resource(f"s{i:02d}", [f"t{i}"], instructions=long_text) for i in range(6)]
    section = server_guidance.render_mcp_server_section(resources, {f"t{i}" for i in range(6)})
    assert len(section) <= server_guidance.MAX_SECTION_CHARS
    assert "x" * server_guidance.MAX_INSTRUCTION_CHARS not in section
    assert "…" in section
    # Every server is still named with its tools, even once instructions no longer fit.
    for i in range(6):
        assert f"### s{i:02d}" in section and f"`t{i}`" in section
    assert section.count("<mcp_server_instructions") < 6

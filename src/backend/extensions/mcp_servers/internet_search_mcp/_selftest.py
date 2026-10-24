"""Offline tool schema smoke test."""

import asyncio

from extensions.mcp_servers.internet_search_mcp.server import mcp


async def check():
    tools = await mcp.list_tools()
    schema = tools[0].input_schema
    assert "queries" in schema["properties"]
    assert "cn_only" not in schema["properties"]
    assert schema["properties"]["max_results"]["maximum"] == 8
    print("SELFTEST_PASS")


if __name__ == "__main__":
    asyncio.run(check())

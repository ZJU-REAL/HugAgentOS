"""Debug MCP env variable injection."""
import asyncio
import os
import sys
import logging

os.environ["AGENTSCOPE_DISABLE_CONSOLE_OUTPUT"] = "true"
logging.disable(logging.CRITICAL)


async def main():
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients

    agent, clients = await create_agent_executor(agent_spec=None, disable_tools=False)

    print(f"Clients: {len(clients)}", flush=True)
    for c in clients:
        print(f"  Client: {c.name}", flush=True)

    # Check if we can call a tool
    from agentscope.message._message_block import ToolUseBlock
    tool_call = ToolUseBlock(
        type="tool_use", id="test-001", name="internet_search",
        input={"query": "test"},
    )

    try:
        tool_res = await agent.toolkit.call_tool_function(tool_call)
        async for chunk in tool_res:
            print(f"  Result: {str(chunk.content)[:300]}", flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)

    # Check the client's env
    for c in clients:
        if hasattr(c, "_env"):
            env = c._env or {}
            tavily = env.get("TAVILY_API_KEY", "NOT SET")
            print(f"  {c.name} TAVILY_API_KEY: {'SET' if tavily and tavily != 'NOT SET' else 'MISSING'}", flush=True)

    try:
        for c in clients:
            await c.close(ignore_errors=True)
    except:
        pass

    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

"""Debug MCP tool execution directly."""
import asyncio
import os
import sys
import logging

os.environ["AGENTSCOPE_DISABLE_CONSOLE_OUTPUT"] = "true"
logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")


async def main():
    from agentscope.mcp import StdIOStatefulClient

    env = {
        "PYTHONPATH": "/app/src/backend",
        "PATH": os.environ.get("PATH", ""),
        "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", ""),
    }

    print("Connecting to internet_search MCP server...", flush=True)
    client = StdIOStatefulClient(
        name="internet_search",
        command="python",
        args=["-m", "mcp_servers.internet_search_mcp.server"],
        env=env,
    )
    await client.connect()

    tools = await client.list_tools()
    print(f"Tools: {[t.name for t in tools]}", flush=True)

    # Get callable function
    func = await client.get_callable_function("internet_search")
    print(f"Function type: {type(func).__name__}", flush=True)

    # Call it directly
    print("Calling internet_search(query='AI policy')...", flush=True)
    try:
        result = await func(query="AI policy 2024")
        print(f"Result type: {type(result).__name__}", flush=True)
        print(f"Result: {result}", flush=True)
        if hasattr(result, "content"):
            print(f"Content: {str(result.content)[:500]}", flush=True)
    except Exception as e:
        import traceback
        print(f"ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()

    await client.close(ignore_errors=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

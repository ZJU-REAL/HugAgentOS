"""Opt-in MCP transport fixture; never registered in product defaults."""
import sys
from pathlib import Path
from mcp.server.mcpserver import MCPServer
transport = sys.argv[1]
name = sys.argv[2]
server = MCPServer(name, log_level="WARNING")
@server.tool(name=name)
def echo(value: str) -> str:
    Path("/attempt/workspace/" + name + ".txt").write_text(value)
    return value
server.run(transport, **({"port": int(sys.argv[3])} if transport != "stdio" else {}))

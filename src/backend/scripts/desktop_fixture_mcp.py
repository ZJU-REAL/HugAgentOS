"""Private loopback MCP fixture for dual_mode_e2e; never imported by the application."""

import json
from pathlib import Path
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

CONFIG = Path("/tmp/capability-e2e-mcp.json")
TRACE = Path("/tmp/capability-e2e-mcp-trace.jsonl")
server = FastMCP(
    "Desktop isolated verification",
    host="127.0.0.1",
    port=31999,
    stateless_http=True,
    json_response=True,
)


@server.tool()
def e2e_read_private_marker() -> str:
    """Read the verification marker held inside the cloud network."""
    config = json.loads(CONFIG.read_text())
    with TRACE.open("a") as stream:
        stream.write(
            json.dumps({"tool": "e2e_read_private_marker", "marker": config["marker"]}) + "\n"
        )
    return config["marker"]


inner = server.streamable_http_app()


async def app(scope, receive, send):
    if scope["type"] == "http":
        import hmac

        config = json.loads(CONFIG.read_text())
        supplied = (
            dict(scope.get("headers") or [])
            .get(b"x-fixture-key", b"")
            .decode("ascii", errors="ignore")
        )
        if not hmac.compare_digest(supplied, config["secret"]):
            return await JSONResponse({"error": "denied"}, status_code=403)(scope, receive, send)
    await inner(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=31999, log_level="warning")

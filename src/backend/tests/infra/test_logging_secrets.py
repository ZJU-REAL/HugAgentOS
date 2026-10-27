"""Public logging sinks redact transport credentials, including exception formatting."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mode", ["development", "production", "fallback"])
def test_transport_logs_and_exceptions_do_not_emit_credentials(mode, tmp_path):
    script = r'''
import logging
from core.infra import logging as config
config.is_production = lambda: MODE == "production"
if MODE == "fallback":
    config.STRUCTLOG_AVAILABLE = False
config.setup_logging()
url = "https://user:SYNTHETIC_PASSWORD@example.invalid/mcp?custom=SYNTHETIC_QUERY#SYNTHETIC_FRAGMENT"
for name in ("httpx2", "httpcore2", "mcp.client.streamable_http"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Redaction must survive diagnostic log levels.
    logger.info("Transport endpoint %s", url)
    logger.debug("Authorization: Bearer SYNTHETIC_BEARER+/=")
    try:
        raise RuntimeError("Transport rejected " + url)
    except RuntimeError:
        logger.exception("MCP operation failed")
try:
    raise RuntimeError("Upstream rejected " + url)
except RuntimeError:
    config.get_logger("acceptance").error("Application failure", exc_info=True)
print("NON_SECRET_SENTINEL")
'''
    log_file = tmp_path / "transport.log"
    result = subprocess.run([sys.executable, "-c", "MODE=" + repr(mode) + script],
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, "LOG_TO_FILE": "true", "LOG_FILE_PATH": str(log_file)})
    assert result.returncode == 0, result.stderr
    assert log_file.exists()
    output = result.stdout + result.stderr + log_file.read_text()
    for secret in ("PASSWORD", "QUERY", "FRAGMENT", "BEARER"):
        assert "SYNTHETIC_" + secret not in output
    assert "example.invalid/mcp" in output
    assert "RuntimeError" in output
    assert "MCP operation failed" in output
    assert "NON_SECRET_SENTINEL" in output


def test_noisy_transport_defaults_still_allow_warnings():
    script = """
import logging
from core.infra.logging import setup_logging
setup_logging()
for name in ("httpx", "httpcore", "httpx2", "httpcore2", "mcp.client"):
    logger = logging.getLogger(name)
    assert logger.getEffectiveLevel() == logging.WARNING, name
    logger.info("SUPPRESSED_TRANSPORT_INFO")
    logger.warning("VISIBLE_TRANSPORT_WARNING")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=15, env={**os.environ, "LOG_TO_FILE": "false"})
    assert result.returncode == 0, result.stderr
    assert "SUPPRESSED_TRANSPORT_INFO" not in result.stdout
    assert result.stdout.count("VISIBLE_TRANSPORT_WARNING") == 5


def test_real_mcp_transport_logs_hide_url_query_credentials():
    script = r'''
import asyncio, json, logging, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from core.infra.logging import setup_logging
from core.llm.mcp.lifetime import discover
setup_logging()
for name in ("httpx2", "httpcore2", "mcp.client"):
    logging.getLogger(name).setLevel(logging.DEBUG)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        method = req.get("method")
        if method == "tools/call" or "id" not in req:
            self.send_response(401 if method == "tools/call" else 202)
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if method == "initialize":
            result = {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "probe", "description": "test", "inputSchema": {"type": "object", "properties": {}}}]}
        else: result = {}
        data = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
async def main():
    resource = await discover("test", {"transport": "streamable_http", "url": f"http://127.0.0.1:{server.server_port}/mcp?custom=SYNTHETIC_MCP_QUERY"})
    try:
        await resource.tools[0].call()
        print("MCP_CALL_COMPLETED")
    finally: await resource.close()
try: asyncio.run(main())
finally: server.shutdown(); server.server_close()
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=20, env={**os.environ, "LOG_TO_FILE": "false"})
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "SYNTHETIC_MCP_QUERY" not in output
    assert "MCP_CALL_COMPLETED" in output
    assert "401" in output

"""Exercise the real OpenSandbox process and private endpoint with disposable state."""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SITE_SCRIPT = """
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        content = '<input aria-label="name"><button onclick="document.body.dataset.result=document.querySelector(\\'input\\').value;document.body.append(document.body.dataset.result)">Submit</button>'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
print(json.dumps({"site_port": server.server_port}), flush=True)
server.serve_forever()
"""

async def start_site(provider, session):
    from core.sandbox.protocol import ProcessRequest
    result = await provider.start_process(ProcessRequest(
        script_content=SITE_SCRIPT, script_name="fixture.py",
        user_id="api_ak_browser_verification", session_id=session, timeout=None,
    ), yield_time_ms=1000)
    for _ in range(30):
        for line in result.get("stdout", result.get("output", "")).splitlines():
            try:
                return json.loads(line)["site_port"]
            except (ValueError, KeyError):
                continue
        assert result.get("exit_code") is None, result
        result = await provider.write_stdin(result["session_id"], sandbox_session_id=session,
            user_id="api_ak_browser_verification", yield_time_ms=1000)
    raise RuntimeError("Fixture site did not start")

async def verify():
    from core.db.engine import Base, engine
    from core.db import models
    Base.metadata.create_all(engine)
    from core.plugins.resources.installation import Installation
    from core.plugins.ui.contract import normalize_ui
    from core.plugins.packaging.importer import manifest_extensions
    from core.sandbox.opensandbox_provider import OpenSandboxProvider
    from core.sandbox.interactive import launch
    from core.plugins.resources.stream import packets
    import httpx
    package = ROOT / "src/backend/plugin_bundles/marketplace/browser-automation"
    manifest = json.loads((package / "plugin.json").read_text())
    ui, dropped = normalize_ui(manifest_extensions(manifest)["ui"])
    assert not dropped
    module = ui["contributes"]["modules"][0]
    installation = Installation("verification", "verification", "browser-automation", ui, package)
    provider = OpenSandboxProvider()
    session = "browser-verification-" + uuid.uuid4().hex
    try:
        site_port = await start_site(provider, session)
        descriptor = await launch(provider, installation, module, "api_ak_browser_verification", session, {
            "token": secrets.token_urlsafe(32), "allowed_hosts": ["127.0.0.1"],
            "chromium_sandbox": True, "idle_seconds": 60,
        })
        async with httpx.AsyncClient(headers=descriptor["headers"], trust_env=False, timeout=40) as client:
            async def command(action, params=None):
                response = await client.post(descriptor["url"] + "/command", json={
                    "id": uuid.uuid4().hex, "actor": "agent", "action": action, "params": params or {}})
                assert response.status_code == 200, "Worker command failed: " + str(response.status_code) + " " + response.text
                return response.json()
            await command("navigate", {"url": f"http://127.0.0.1:{site_port}/"})
            await command("fill", {"role": "textbox", "name": "name", "text": "云沙箱实机验证"})
            await command("click", {"role": "button", "name": "Submit"})
            assert "云沙箱实机验证" in (await command("snapshot"))["snapshot"]
            screenshot = await command("screenshot")
            assert screenshot["data"]
            async with client.stream("GET", descriptor["url"] + "/events") as response:
                assert response.status_code == 200
                async for packet in packets(response.aiter_bytes()):
                    length = int.from_bytes(packet[:4], "big")
                    header = json.loads(packet[4:4+length])
                    if header["type"] == "frame":
                        assert len(packet) > 4 + length
                        break
            assert (await client.post(descriptor["url"] + "/shutdown")).status_code == 200
        print(json.dumps({"checks": ["Real OpenSandbox managed Python worker and private port proxy",
            "Chromium navigates, fills Chinese form and clicks",
            "Screenshot and binary live-frame stream",
            "Bound endpoint shutdown and disposable sandbox cleanup"]}))
    finally:
        await provider.close_session(session)
        await provider.shutdown()

def main():
    image = os.environ["BROWSER_E2E_OPENSANDBOX_IMAGE"]
    domain = os.environ["BROWSER_E2E_OPENSANDBOX_DOMAIN"]
    with tempfile.TemporaryDirectory(prefix="browser-cloud-e2e-") as folder:
        env = {"DATABASE_URL": "sqlite:///" + str(Path(folder) / "test.db"),
            "HUGAGENT_HOME": folder, "STORAGE_PATH": str(Path(folder)/"storage"),
            "DEPLOY_PROFILE": "team", "AUTH_MODE": "mock", "SANDBOX_PROVIDER": "opensandbox",
            "OPENSANDBOX_DOMAIN": domain, "OPENSANDBOX_IMAGE": image,
            "OPENSANDBOX_DIRECT_EXECD": "false", "OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED": "false",
            "OPENSANDBOX_SNAPSHOT_ENABLED": "false", "HOST_STORAGE_PATH": "",
            "SCRIPT_RUNNER_WORKSPACE": "/workspace", "REDIS_URL": "",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        for name in ("DWS", "LARK", "EMAIL", "YIDA"):
            env[name + "_CREDS_BIND_MOUNT_ENABLED"] = "false"
        for name in ("JUPYTER", "LIGHT"):
            env["OPENSANDBOX_POOL_" + name + "_MIN_IDLE"] = "0"
        os.environ.update(env)
        sys.path.insert(0, str(ROOT / "src/backend"))
        asyncio.run(verify())

if __name__ == "__main__":
    main()

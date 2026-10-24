"""Isolated real browser E2E fixture; never uses production DB or services."""
import os
from pathlib import Path
home = Path(os.environ["BROWSER_E2E_HOME"])
os.environ["DATABASE_URL"] = "sqlite:///" + str(home / "test.db")
os.environ["HUGAGENT_HOME"] = str(home)
os.environ["LOGIN_MODE"] = "local"
os.environ["DEPLOY_PROFILE"] = "local"
os.environ["SANDBOX_PROVIDER"] = "script_runner"
os.environ["BROWSER_ALLOWED_HOSTS"] = "127.0.0.1"
os.environ.setdefault("BROWSER_CHROMIUM_SANDBOX", "true")
os.environ["MCP_HOST"] = "127.0.0.1"
os.environ["HUGAGENT_CAPS_ROOT"] = str(home)

from features.desktop.bridge import gateway as bridge_gateway
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from core.db.engine import Base, engine, SessionLocal
from core.db import models
from core.auth.backend import UserContext, get_current_user
from core.capabilities.plugins.management.installation import install_plugin
from api.routes.v1 import plugin_resources
from features.workbench.api import plugin_ui
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(models.UserShadow(user_id="browser-owner", user_center_id="browser-owner", username="Browser owner"))
    db.add(models.ChatSession(chat_id="browser-chat", user_id="browser-owner"))
    db.commit()
    from core.auth.users import UserService
    UserService(db).update_user_metadata("browser-owner", {"tool_approval_mode": os.getenv("BROWSER_E2E_APPROVAL_MODE", "auto")})
    install_plugin(db, "browser-automation", owner_user_id="browser-owner")

from core.capabilities.plugins.resources.lifecycle import lifespan as resources_lifespan
app = FastAPI(lifespan=resources_lifespan)
app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="browser-owner", user_center_id="browser-owner", username="Browser owner")
app.include_router(plugin_resources.router, prefix="/api")
app.include_router(plugin_ui.router, prefix="/api")
from api.routes import files
app.include_router(files.router)
for route in files.router.routes:
    if not hasattr(route, "dependant"):
        continue
    for dependency in route.dependant.dependencies:
        if dependency.name == "user":
            app.dependency_overrides[dependency.call] = app.dependency_overrides[get_current_user]

@app.get("/health")
def health():
    return {"ok": True}

from urllib.parse import urlsplit
from core.config.mcp_config import _PORTS
from core.config.settings import settings
_PORTS["browser_runtime"] = urlsplit(os.environ["BROWSER_E2E_MCP"]).port
assert settings.server.mcp_host == "127.0.0.1"
from features.desktop.local.plugins import service as local_packages
from features.desktop.capabilities import local_plugin_runtime, skills
modern = local_packages.install("browser-owner", str(Path(__file__).parents[1] / "extensions/plugins/marketplace/browser-automation"))
skill_account.current_local_user_id = lambda: "browser-owner"
# The isolated desktop proxy carries this synthetic account. Preserve the
# real bridge authentication and ticket revalidation with its identity seam.
from features.desktop.bridge import client as desktop_bridge
bridge_identity.get_identity_state = lambda: {"shell_user_center_id": "browser-owner"}

@app.post("/test/tool/{name}")
async def tool(name: str, arguments: dict):
    from core.llm.factory.tools.mcp_config import inject_runtime_headers as _inject_runtime_headers
    from core.llm.mcp.client import make_client
    from core.capabilities.catalog.resolver import _owned_enabled_ids
    from features.desktop.bridge.gateway import cloud_gateway_mcp_configs
    sid = modern["components"]["mcp"][0]
    with SessionLocal() as db:
        _, allowed = _owned_enabled_ids(db, "browser-owner", {})
    assert sid in allowed
    choices = []
    cloud_gateway_mcp_configs(allowed, resolution_out=choices)
    assert sid in choices[0].chosen
    config = _inject_runtime_headers(local_plugin_runtime.configs("browser-owner"),
        current_user_id="browser-owner", chat_id="browser-chat")[sid]
    async with make_client(sid, config) as client:
        result = await client.call_tool(name, arguments)
    if result.is_error:
        return __import__("fastapi").responses.JSONResponse(status_code=409, content={"error": result.content[0].text})
    try:
        return json.loads(result.content[0].text)
    except (ValueError, AttributeError):
        return __import__("fastapi").responses.JSONResponse(status_code=409, content={"error": str(result.content)})

@app.post("/test/cloud-proof")
async def cloud_proof():
    import mcp.types
    from features.desktop.capability.mcp import invoke_gateway_tool
    from features.desktop.capability import mcp as gateway
    gateway.guard_capability_content = lambda uid, data, **kw: data
    async with streamable_http_client(os.environ["BROWSER_E2E_MCP"]) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {t.name: t.model_dump() for t in (await client.list_tools()).tools}
    async def call(name, args):
        result = await invoke_gateway_tool({"user_id":"browser-owner", "server_id":"cloud-browser",
            "target":{"url":os.environ["BROWSER_E2E_MCP"],"transport":"streamable_http"},
            "tool":tools[name]}, args, {"x-current-user-id":"spoofed",
            "x-chat-id":"browser-chat", "x-hugagent-invocation":"different-device-key",
            "x-hugagent-plugin-id":"plugin:local:untrusted"})
        assert not result.get("metadata", {}).get("is_error"), result
        return result["content"][0]["text"]
    # A nonexistent disposable ID must reach the authenticated callback, which
    # returns resource_unavailable rather than an MCP/proof authentication error.
    result = await call("browser_observe", {"resource_id":"nonexistent-proof-fixture", "action":"state"})
    assert "resource_unavailable" in result and "not_authorized" not in result, result
    return {"fresh_cloud_proof": True, "device_source_not_forwarded": True}

@app.get("/fixture/{asset}")
def fixture_asset(asset: str):
    if asset not in {"fixture.js", "fixture.css"}:
        raise HTTPException(404)
    return FileResponse(Path(os.environ["BROWSER_E2E_ASSETS"]) / asset)

@app.get("/")
def fixture():
    return HTMLResponse('<html><head><link rel="stylesheet" href="/fixture/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture/fixture.js"></script></body></html>')

@app.get("/test/site")
def site():
    return HTMLResponse('''<html><body style="margin:24px;background:white;color:black">
<label for="name">姓名</label><input id="name" style="width:240px;height:36px"><button id="submit" onclick="document.querySelector('#result').textContent=document.querySelector('#name').value;window.count=(window.count||0)+1">提交</button>
<p id="result"></p><button id="dialog" onclick="alert('请确认浏览器可用')">弹窗</button>
<input type="file" id="upload" onchange="document.querySelector('#filename').textContent=this.files[0].name"><p id="filename"></p>
<a id="newtab" href="/test/cookie" target="_blank">新页面</a>
<a id="download" href="data:text/plain;charset=utf-8,browser-download" download="browser.txt">下载</a>
<div id="scrollpanel" style="position:absolute;left:200px;top:250px;width:200px;height:300px;overflow:auto" onscroll="document.querySelector('#scroll-result').textContent=Math.round(this.scrollTop)"><div style="height:3000px">Scrollable content</div></div><p id="scroll-result">0</p>
<script>window.count=0;document.cookie='browser_login=verified;path=/';</script></body></html>''')

@app.get("/test/cookie")
def cookie():
    return HTMLResponse("<html><body><p id='cookie'></p><script>document.querySelector('#cookie').textContent=document.cookie;</script></body></html>")

@app.post("/test/confinement")
async def confinement_probe():
    from core.sandbox.factory import get_sandbox_provider
    from core.sandbox.protocol import ProcessRequest
    from core.capabilities.plugins.resources.confinement import launch_policy
    from core.sandbox.workspace.resolution import workspace_directory
    private = home / "credential-probe"
    private.write_text("isolated-secret-probe")
    workspace = Path(workspace_directory("browser-chat"))
    script = """import json
from pathlib import Path
blocked = False
try:
    Path(%r).read_text()
except (PermissionError, FileNotFoundError):
    blocked = True
Path(%r).write_text("verified")
print(json.dumps({"private_read_blocked": blocked, "workspace_write": True}))
""" % (str(private), str(workspace / "write-probe"))
    provider = get_sandbox_provider()
    result = await provider.start_process(ProcessRequest(
        script_content=script, script_name="permission_probe.py",
        user_id="browser-owner", session_id="browser-chat",
        sandbox_launch=launch_policy(provider, "browser-owner", "browser-chat"),
    ), yield_time_ms=1000)
    if result.get("exit_code") != 0:
        raise HTTPException(500, "permission_probe_failed: " + str(result))
    return json.loads(result["stdout"])


@app.post("/test/prewarm")
async def prewarm_probe():
    import asyncio
    from core.sandbox.factory import get_sandbox_provider
    from core.capabilities.plugins.resources import store
    await get_sandbox_provider().put_file("browser-chat", ".prewarm-probe", b"", user_id="browser-owner")
    for _ in range(600):
        with SessionLocal() as db:
            row = db.query(store.PluginResource).filter_by(
                chat_id="browser-chat", user_id="browser-owner", status="warm").first()
            if row is not None:
                return {"resource_id": row.resource_id, "status": row.status}
        await asyncio.sleep(.05)
    raise HTTPException(500, "sandbox did not produce a warm browser")


@app.post("/test/synced-tool/{name}")
async def synced_tool(name: str, arguments: dict):
    """Actual cloud package export, device binding and MCP callback; no cloud account."""
    from features.desktop.capabilities import registry, store, device_catalog, skills
    from core.capabilities.reference import ResourceRef
    from features.desktop.capability.entities import _plugin_files
    from core.capabilities.manifest_protocol import entity_content_hash
    from core.llm.factory.tools.mcp_config import inject_runtime_headers as _inject_runtime_headers
    from core.llm.mcp.client import make_client
    from features.desktop.bridge import client as bridge
    profile = "browser-cloud-fixture"
    with SessionLocal() as db:
        row = db.query(models.InstalledPlugin).filter_by(slug="browser-automation").first()
        files = _plugin_files({"install_id": row.install_id, "slug": row.slug, "name": row.name,
            "components": row.component_ids, "ui_contributions": row.ui_contributions,
            "import_report": row.import_report})
    digest = entity_content_hash(files)
    comp = store.write_from_files("plugin", profile, "browser-automation", digest[:12], files)
    sid = next(iter(json.loads(files["plugin.json"])["native_servers"]))
    inst = registry.upsert(profile_id=profile, ref=ResourceRef("fixture", "account", "plugin", "browser-automation"),
        content_hash=digest, source="cloud", enabled=True, payload={"components": {"mcp": [sid]}})
    registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
    skill_account.current_account_profile = lambda: profile
    skill_account.account_authorized_for = lambda uid: uid == "browser-owner"
    device_catalog._profile = lambda: profile
    # The cloud side supplies its authorized schema; its URL must never be used.
    async with streamable_http_client(os.environ["BROWSER_E2E_MCP"]) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            schemas = [t.model_dump() for t in (await client.list_tools()).tools]
    bridge_gateway.bridge_context = lambda: {"state": {"cloud_base": "https://cloud.invalid"},
        "manifest_revision": "fixture", "profile": profile,
        "servers": [{"server_id": sid, "tools": schemas, "source_plugin": "browser-automation"}]}
    cfg = bridge_gateway.cloud_gateway_mcp_configs([sid])[sid]
    assert cfg["execution_scope"] == "local" and "gateway_invoke_url" not in cfg
    chat_id = arguments.pop("_chat_id", "browser-chat")
    cfg = _inject_runtime_headers({sid: cfg}, current_user_id="browser-owner", chat_id=chat_id)[sid]
    async with make_client(sid, cfg) as client:
        result = await client.call_tool(name, arguments)
    text = result.content[0].text
    if "conversation_unavailable" in text:
        return {"error": "conversation_unavailable"}
    if result.is_error:
        raise HTTPException(409, text)
    payload = json.loads(text)
    if name == "browser_open":
        resource = payload["resource"]
        assert resource["execution_scope"] == "local" and resource["install_id"] == inst.install_id
        with SessionLocal() as db:
            owned = db.get(models.PluginResource, resource["resource_id"])
            assert owned.user_id == "browser-owner" and owned.chat_id == "browser-chat"
    return payload

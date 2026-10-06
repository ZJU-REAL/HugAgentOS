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

import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from core.db.engine import Base, engine, SessionLocal
from core.db import models
from core.auth.backend import UserContext, get_current_user
from core.plugins.management.installation import install_plugin
from api.routes.v1 import plugin_resources, plugin_ui
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(models.UserShadow(user_id="browser-owner", user_center_id="browser-owner", username="Browser owner"))
    db.add(models.ChatSession(chat_id="browser-chat", user_id="browser-owner"))
    db.commit()
    from core.services.user_service import UserService
    UserService(db).update_user_metadata("browser-owner", {"tool_approval_mode": os.getenv("BROWSER_E2E_APPROVAL_MODE", "auto")})
    install_plugin(db, "browser-automation", owner_user_id="browser-owner")

app = FastAPI()
app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="browser-owner", user_center_id="browser-owner", username="Browser owner")
app.include_router(plugin_resources.router, prefix="/api")
app.include_router(plugin_ui.router, prefix="/api")
from api.routes import files
app.include_router(files.router)
for route in files.router.routes:
    for dependency in route.dependant.dependencies:
        if dependency.name == "user":
            app.dependency_overrides[dependency.call] = app.dependency_overrides[get_current_user]

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/test/tool/{name}")
async def tool(name: str, arguments: dict):
    from core.llm.mcp_invocation import issue
    headers = {"x-current-user-id": "browser-owner", "x-chat-id": "browser-chat", **issue("browser_runtime", "browser-owner", "browser-chat")}
    async with streamablehttp_client(os.environ["BROWSER_E2E_MCP"], headers=headers) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(name, arguments)
            if result.isError:
                return __import__("fastapi").responses.JSONResponse(status_code=409, content={"error": result.content[0].text})
            value = json.loads(result.content[0].text)
            return value

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
    from core.plugins.resources.confinement import launch_policy
    from core.llm.tools._paths import workspace_directory
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
        raise HTTPException(500, "permission_probe_failed")
    return json.loads(result["stdout"])

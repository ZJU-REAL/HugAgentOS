"""Loopback-only command E2E server with a private database and real processes."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--directory", type=Path, required=True)
args = parser.parse_args()
root = args.directory.resolve()
root.mkdir(parents=True, exist_ok=False)
os.environ.update(
    DATABASE_URL="sqlite:///" + str(root / "commands.sqlite"),
    REDIS_URL="", SESSION_STORE="memory", AUTH_MODE="mock",
    DEPLOY_PROFILE="local", STORAGE_TYPE="local", STORAGE_PATH=str(root / "storage"),
    HUGAGENT_HOME=str(root), SANDBOX_SKILLS_DIR=str(root / "skills"),
    SCRIPT_RUNNER_WORKSPACE=str(root / "workspace"), SANDBOX_RUNNER_TOKEN="",
    SANDBOX_PROVIDER="script_runner", SANDBOX_TOOLS_ENABLED="true",
    SANDBOX_RUNNER_URL=f"http://127.0.0.1:{args.port}/runner",
    NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
)

from contextlib import asynccontextmanager

import uvicorn
from api.routes.v1.commands import router
from api.startup.commands import shutdown_commands
from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, SessionLocal, engine
from core.db.models import ChatSession, UserShadow
from core.sandbox.commands.runtime import runtime
from core.sandbox.protocol import ProcessRequest
from core.sandbox.providers.script_runner.provider import ScriptRunnerProvider
from core.sandbox.runner import server
from fastapi import FastAPI
from pydantic import BaseModel

Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(UserShadow(user_id="e2e", username="synthetic"))
    db.flush()
    db.add(ChatSession(chat_id="e2e-chat", user_id="e2e", title="command e2e"))
    db.commit()

provider = ScriptRunnerProvider()

@asynccontextmanager
async def lifespan(app):
    yield
    await shutdown_commands()
    await server.process_sessions.close_all()

app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(server.app.router, prefix="/runner")
app.dependency_overrides[get_current_user] = lambda: UserContext(
    user_id="e2e", user_center_id="e2e", username="synthetic")

class Start(BaseModel):
    command: str

@app.get("/test/ping")
async def ping():
    return {"ok": True}

@app.post("/test/start")
async def start(body: Start):
    request = ProcessRequest(script_content=body.command, script_name="e2e.sh",
        language="bash", session_id="e2e-chat", user_id="e2e")
    return await runtime.start(provider, request, chat_id="e2e-chat",
                               source_run_id=None, background=True)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")

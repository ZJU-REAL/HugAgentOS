"""Owner-only local client session API; never available on a cloud deployment."""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from core.auth.backend import UserContext, get_current_user
from core.config.local_mode import local_mode_enabled
from core.db.engine import get_db
from core.db.models import ChatSession, ChatRun, Project
from core.db.threads import run_in_session
from core.infra.background import spawn
from core.infra.responses import success_response
from integrations.agent_client import registry

router = APIRouter(prefix="/v1/client-sessions", tags=["Client sessions"])

_ACTIVE_RUN = ("pending", "queued", "running")


class ServerConfig(BaseModel):
    transport: Literal["stdio", "streamable_http", "sse"]
    command: str | None = Field(None, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=128)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    url: str | None = Field(None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def validate_transport(self):
        if self.transport == "stdio":
            if not self.command or self.url:
                raise ValueError("stdio requires a command and no URL")
        else:
            url = urlsplit(self.url or "")
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or self.command:
                raise ValueError("HTTP MCP requires an HTTP(S) URL without embedded credentials")
        return self


class Registration(BaseModel):
    cwd: str = Field(max_length=4096)
    mode: Literal["ask", "auto", "full"] = "ask"
    servers: dict[str, ServerConfig] = Field(default_factory=dict, max_length=32)


class Renewal(BaseModel):
    lease_id: str = Field(min_length=32, max_length=32)
    mode: Literal["ask", "auto", "full"] | None = None


def owner(db, user, chat_id):
    if not local_mode_enabled() or user.public_chat_visitor or user.api_key_agent_id or user.api_key_project_id:
        raise HTTPException(403, "Client sessions require an unrestricted local account")
    chat = db.get(ChatSession, chat_id)
    if chat is None or str(chat.user_id) != str(user.user_id) or chat.deleted_at is not None:
        raise HTTPException(404, "Session not found")
    return chat


def _has_active_run(db, chat_id) -> bool:
    return db.query(ChatRun).filter(ChatRun.chat_id == chat_id, ChatRun.status.in_(_ACTIVE_RUN)).first() is not None


def conflict(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _workspace(db, user, chat_id, cwd) -> str:
    """The chat's own local project folder, which the client must be working in."""
    chat = owner(db, user, chat_id)
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise HTTPException(422, "cwd must be an existing absolute directory")
    project = db.get(Project, chat.project_id) if chat.project_id else None
    stored = (((project.extra_data or {}).get("local") or {}).get("path")) if project else None
    if not project or project.deleted_at or project.kind != "local" or not stored or Path(stored).resolve() != path.resolve():
        raise HTTPException(409, "Client workspace must match the chat local project")
    if _has_active_run(db, chat_id):
        raise HTTPException(409, "Cannot attach client session to an active run")
    return str(path.resolve())


@router.post("/{chat_id}")
async def register(chat_id: str, body: Registration, user: UserContext = Depends(get_current_user)):
    cwd = await run_in_session(_workspace, user, chat_id, body.cwd)
    configs = {name: value.model_dump(exclude_none=True) for name, value in body.servers.items()}
    for value in configs.values():
        if value["transport"] == "stdio":
            value["cwd"] = cwd
    user_id = str(user.user_id)
    lease_id = conflict(lambda: registry.create(user_id, chat_id, cwd, body.mode, configs))
    spawn(registry.watch(user_id, chat_id, lease_id), name="client_session_watch")
    return success_response(data={"lease_id": lease_id, "ttl_seconds": registry.TTL_SECONDS})


@router.patch("/{chat_id}")
def renew(chat_id: str, body: Renewal, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    owner(db, user, chat_id)
    previous = conflict(lambda: registry.require(str(user.user_id), chat_id, body.lease_id))
    if body.mode is not None and previous.mode != body.mode and _has_active_run(db, chat_id):
        raise HTTPException(409, "Cannot change permissions during a run")
    conflict(lambda: registry.update(str(user.user_id), chat_id, body.lease_id, body.mode))
    return success_response(data={"ttl_seconds": registry.TTL_SECONDS})


@router.delete("/{chat_id}")
async def release(chat_id: str, lease_id: str, user: UserContext = Depends(get_current_user)):
    await run_in_session(owner, user, chat_id)
    conflict(lambda: registry.require(str(user.user_id), chat_id, lease_id))
    await registry.revoke(str(user.user_id), chat_id, lease_id)
    return success_response(data={"released": True})

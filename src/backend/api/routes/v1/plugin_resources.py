"""Generic plugin resource lifecycle and attachment routes."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import success_response
from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.stream import connect

from core.capabilities.plugins.resources.lifecycle import lifespan
router = APIRouter(prefix="/v1", tags=["Plugin resources"], lifespan=lifespan)

class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=128)
    resource_id: str | None = None
    install_id: str | None = None
    checkpoint_id: str | None = None

class ToolRequest(OpenRequest):
    slug: str
    module_id: str
    action: str = "open"
    params: dict = Field(default_factory=dict)
    command_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

@router.get("/plugins/{slug}/checkpoints")
def list_checkpoints(slug: str, install_id: str | None = None, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    from core.capabilities.plugins.resources.installation import resolve
    selected = resolve(db, slug, str(user.user_id), install_id)
    rows = db.query(store.PluginResourceCheckpoint).filter_by(user_id=str(user.user_id), install_id=selected.install_id).all()
    return success_response(data={"items": [{"checkpoint_id": row.checkpoint_id, "name": row.name} for row in rows]})

@router.delete("/plugin-checkpoints/{checkpoint_id}")
def delete_checkpoint(checkpoint_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(store.PluginResourceCheckpoint, checkpoint_id)
    if row is None or row.user_id != str(user.user_id):
        raise HTTPException(404, "checkpoint_unavailable")
    db.delete(row)
    db.commit()
    return success_response(data={"deleted": True})

@router.post("/plugins/{slug}/resources/{module_id}")
async def open_resource(slug: str, module_id: str, body: OpenRequest, user: UserContext = Depends(get_current_user)):
    result = await service.create(slug, module_id, str(user.user_id), **body.model_dump())
    return success_response(data={"resource": result})

def _chat_resource_ids(db, chat_id, uid):
    from core.db.models import ChatSession
    chat = db.get(ChatSession, chat_id)
    if chat is None or chat.user_id != uid or chat.deleted_at is not None:
        raise HTTPException(404, "conversation_unavailable")
    rows = db.query(store.PluginResource.resource_id).filter_by(user_id=uid, chat_id=chat_id, status="active").limit(8)
    return [resource_id for (resource_id,) in rows]

@router.get("/chats/{chat_id}/plugin-resources")
async def chat_resources(chat_id: str, user: UserContext = Depends(get_current_user)):
    import asyncio
    uid = str(user.user_id)
    resource_ids = await run_in_session(_chat_resource_ids, chat_id, uid)
    async def live(resource_id):
        try:
            row = await run_in_session(service.authorized_row, resource_id, uid)
            state = await asyncio.wait_for(service.request(row, "/state"), 5)
            if state.get("closed"):
                return None
            return store.public(row)
        except (HTTPException, TimeoutError):
            return None
    items = await asyncio.gather(*(live(resource_id) for resource_id in resource_ids))
    return success_response(data={"items": [item for item in items if item is not None]})

@router.get("/plugin-resources/{resource_id}")
async def get_resource(resource_id: str, user: UserContext = Depends(get_current_user)):
    row = await run_in_session(service.authorized_row, resource_id, str(user.user_id))
    state = await service.request(row, "/state")
    return success_response(data={"resource": store.public(row), "state": state})

@router.post("/plugin-resources/{resource_id}/attach")
def attach_resource(resource_id: str, request: Request, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    row = service.authorized(db, resource_id, str(user.user_id))
    from urllib.parse import urlsplit
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(403, "invalid_origin")
    from core.capabilities.plugins.resources.authentication import capture
    return success_response(data={"ticket": store.ticket(db, row, origin, capture(request))})

@router.post("/plugin-resources/{resource_id}/asset-ticket")
def issue_asset_ticket(resource_id: str, request: Request, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    row = service.authorized(db, resource_id, str(user.user_id))
    from core.capabilities.plugins.resources.installation import resolve
    from features.workbench.ui.contract import find_module
    if row.application_key is not None:
        from core.capabilities.plugins.resources.owners import resolve_installation
        selected = resolve_installation(db, row, str(user.user_id))
    else:
        selected = resolve(db, row.slug, str(user.user_id), row.install_id)
    module = find_module(selected.ui, row.module_id)
    if module is None:
        raise HTTPException(404, "resource_module_unavailable")
    from core.capabilities.plugins.resources.authentication import capture
    return success_response(data={"ticket": store.asset_ticket(db, row, capture(request)), "entry": module["entry"]})

def _asset_response(db, execution_scope, resource_id, user_id, asset_path):
    from features.workbench.api.plugin_ui import web_asset_response
    row = service.authorized(db, resource_id, user_id)
    if row.scope != execution_scope:
        raise HTTPException(409, "execution_scope_mismatch")
    if row.application_key is not None:
        from core.capabilities.plugins.resources.owners import resolve_installation
        selected = resolve_installation(db, row, user_id)
        response = web_asset_response(db, row.slug, user_id, asset_path, row.install_id,
                                      _installation=selected)
    else:
        response = web_asset_response(db, row.slug, user_id, asset_path, row.install_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Allow-Origin"] = "null"
    return response

@router.get("/plugin-resource-assets/{execution_scope}/{ticket}/{asset_path:path}")
async def resource_asset(execution_scope: str, ticket: str, asset_path: str):
    # Null-origin iframe subresources cannot carry SameSite login cookies.
    # The ticket grants static package reads only; its saved session is still revocable.
    from core.capabilities.plugins.resources.authentication import validate
    resource_id, user_id, authentication = await run_in_session(store.resolve_asset_ticket, ticket)
    await validate(authentication, user_id)
    return await run_in_session(_asset_response, execution_scope, resource_id, user_id, asset_path)

@router.websocket("/plugin-resources/{resource_id}/stream")
async def resource_stream(websocket: WebSocket, resource_id: str):
    await connect(websocket, resource_id)

@router.delete("/plugin-resources/{resource_id}")
async def close_resource(resource_id: str, user: UserContext = Depends(get_current_user)):
    row = await run_in_session(service.authorized_row, resource_id, str(user.user_id))
    await service.close(row)
    return success_response(data={"closed": True})

@router.post("/plugin-resources/{resource_id}/downloads/{download_id}/retain")
async def retain_download(resource_id: str, download_id: str, user: UserContext = Depends(get_current_user)):
    from core.capabilities.plugins.resources.artifacts import retain
    row = await run_in_session(service.authorized_row, resource_id, str(user.user_id))
    return success_response(data=await retain(row, download_id))

@router.get("/plugin-resources/{resource_id}/downloads/{download_id}")
async def download_resource(resource_id: str, download_id: str, user: UserContext = Depends(get_current_user)):
    import httpx
    row = await run_in_session(service.authorized_row, resource_id, str(user.user_id))
    target = store.descriptor(row)
    client = httpx.AsyncClient(timeout=60, headers=target["headers"], trust_env=False)
    response = await client.send(client.build_request("GET", target["url"] + "/download/" + download_id), stream=True)
    if response.status_code != 200:
        await response.aclose()
        await client.aclose()
        raise HTTPException(response.status_code, "download_unavailable")
    async def content():
        try:
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise ValueError("download_limit")
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
    return StreamingResponse(content(), media_type="application/octet-stream", headers={"Content-Disposition": response.headers.get("content-disposition", "attachment"), "Cache-Control": "no-store"})

@router.post("/internal/plugin-resources/tool")
async def tool_resource(body: ToolRequest, request: Request):
    from features.sites.api.internal_site_auth import _check_internal_token
    _check_internal_token(request.headers.get("x-internal-token"))
    from core.llm.mcp.invocation import verify
    from extensions.mcp_servers._ports import PORTS
    audience = request.headers.get("x-hugagent-mcp-audience", "")
    if audience not in PORTS or request.headers.get("x-chat-id") != body.chat_id:
        raise HTTPException(401, "invocation_context_required")
    try:
        claims = verify(request.headers, audience)
    except ValueError as exc:
        raise HTTPException(401, "invocation_not_authorized") from exc
    signed_install = claims.get("plugin", "")
    if signed_install and signed_install != body.install_id:
        raise HTTPException(403, "plugin_binding_mismatch")
    user_id = request.headers.get("x-current-user-id", "")
    if not user_id:
        raise HTTPException(401, "identity_required")
    if body.action == "open":
        result = await service.create(body.slug, body.module_id, user_id, body.chat_id, resource_id=body.resource_id, install_id=body.install_id, checkpoint_id=body.checkpoint_id)
        return success_response(data={"resource": result})
    if not body.resource_id:
        raise HTTPException(400, "resource_required")
    row = await run_in_session(service.authorized_row, body.resource_id, user_id)
    if (row.chat_id != body.chat_id or row.slug != body.slug or row.module_id != body.module_id
            or (signed_install and row.install_id != signed_install)):
        raise HTTPException(403, "resource_binding_mismatch")
    if body.action == "close":
        await service.command(row, {"id": body.command_id, "action": "close", "params": {}}, actor="agent")
        await service.close(row)
        return success_response(data={"closed": True})
    if body.action == "retain_download":
        await service.command(row, {"id": uuid.uuid4().hex, "action": "state", "params": {}}, actor="agent")
        from core.capabilities.plugins.resources.artifacts import retain
        return success_response(data=await retain(row, str(body.params["download_id"])))
    data = await service.command(row, {"id": body.command_id, "action": body.action, "params": body.params}, actor="agent")
    return success_response(data=data)

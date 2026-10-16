"""Authenticated host broker. Opaque plugin frames never receive approval credentials."""

from core.applications.coordination import runtime


import asyncio
import json
from typing import Any
from urllib.parse import quote, urlencode

from core.applications import capabilities as grants
from core.applications import external, application_config
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from core.plugins.ui.contract import find_data_source
from core.plugins.ui.data_proxy import call_data_source
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session


async def trusted_host(request: Request):
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if request.headers.get("sec-fetch-site") != "same-origin" or (
        origin != expected and not (request.method == "GET" and origin is None)
    ):
        external.fail(403, "host_origin_required")
    if request.headers.get("content-type", "").split(";")[0] == "application/json":
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > 72 * 1024:
                external.fail(413, "request_too_large")
            chunks.append(chunk)
        request._body = b"".join(chunks)
        if not request._body:
            # Query-only host requests can carry a default JSON header.
            # Required write bodies are still enforced by the route schema.
            return
        try:
            value = json.loads(request._body)
        except (ValueError, RecursionError):
            external.fail(422, "invalid_json")
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if nodes > 4096 or depth > 16:
                external.fail(413, "request_too_complex")
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)


def host_identity(user: UserContext = Depends(require_auth())):
    if user.api_key_id:
        external.fail(403, "host_session_required")
    return user


class CapabilityRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                await trusted_host(request)
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "invalid_request"}) from None

        return safe


router = APIRouter(
    route_class=CapabilityRoute,
    prefix="/applications/{key}/capabilities",
    tags=["workbench capabilities"],
)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    instanceId: str | None = Field(default=None, pattern="^[A-Za-z0-9_-]{16,64}$")
    capabilities: list[str] = Field(max_length=32)
    moduleId: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    instanceId: str | None = Field(default=None, pattern="^[A-Za-z0-9_-]{16,64}$")
    approvalId: str = Field(min_length=20, max_length=100)


class Query(Operation):
    sourceId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    params: dict[str, Any] = Field(default_factory=dict)


class Check(Operation):
    capability: str = Field(min_length=1, max_length=80)


class Selection(Operation):
    fileId: str = Field(min_length=1, max_length=128)


@router.post("/approval")
def approve(
    key: str,
    body: Approval,
    response: Response,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return success_response(
        data=grants.approve(
            db, user.user_id, key, body.revision, body.capabilities, body.moduleId, body.instanceId
        )
    )


@router.post("/check")
def check(
    key: str,
    body: Check,
    response: Response,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    _, _, _, effective = grants.authorized(
        db, user.user_id, key, body.revision, body.approvalId, body.capability, body.instanceId
    )
    response.headers["Cache-Control"] = "no-store"
    return success_response(data={"effective": sorted(effective)})


@router.delete("/approval/{approval_id}")
def revoke(
    key: str,
    approval_id: str,
    revision: str,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    grants.revoke(db, user.user_id, key, revision, approval_id)
    return success_response(data={"revoked": True})


@router.post("/data/query")
async def query(
    key: str,
    body: Query,
    response: Response,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    if len(json.dumps(body.params).encode()) > 64 * 1024:
        external.fail(413, "request_too_large")
    lease, selected, effective = grants.reserve(
        db, user.user_id, key, body.revision, body.approvalId, "data.query", body.instanceId
    )
    try:
        if "data_source:" + body.sourceId not in effective:
            external.fail(403, "data_source_denied")
        source = find_data_source(selected.ui, body.sourceId)
        if source is None:
            external.fail(404, "data_source_not_found")
        try:
            result = await asyncio.wait_for(
                call_data_source(
                    source,
                    body.params,
                    slug=selected.slug,
                    config_values=application_config.data_values(db, key, selected),
                    configured_origins=application_config.trusted_origins(db, key, selected),
                ),
                timeout=30,
            )
        except TimeoutError:
            external.fail(504, "capability_timeout")
        grants.authorized(
            db, user.user_id, key, body.revision, body.approvalId, "data.query", body.instanceId
        )
        if len(json.dumps(result).encode()) > 256 * 1024:
            external.fail(413, "response_too_large")
        response.headers["Cache-Control"] = "no-store"
        return success_response(data=result)
    finally:
        grants.release(db, body.approvalId, lease)


def file_info(artifact):
    return {
        "fileId": artifact.artifact_id,
        "name": artifact.filename,
        "size": artifact.size_bytes,
        "mimeType": artifact.mime_type,
    }


@router.post("/files/select")
def select(
    key: str,
    body: Selection,
    response: Response,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    artifact = grants.select_file(
        db,
        user.user_id,
        key,
        body.revision,
        body.approvalId,
        body.fileId,
        instance_id=body.instanceId,
    )
    response.headers["Cache-Control"] = "no-store"
    return success_response(
        data={
            **file_info(artifact),
            "resourceUrl": "/v1/workbench/applications/"
            + quote(key, safe="")
            + "/capabilities/files/"
            + quote(body.fileId, safe="")
            + "?"
            + urlencode(
                {
                    "revision": body.revision,
                    **({"instanceId": body.instanceId} if body.instanceId else {}),
                }
            ),
        }
    )


@router.post("/files/resource")
def resource(
    key: str,
    body: Selection,
    response: Response,
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    artifact = grants.select_file(
        db,
        user.user_id,
        key,
        body.revision,
        body.approvalId,
        body.fileId,
        "file.save",
        body.instanceId,
    )
    response.headers["Cache-Control"] = "no-store"
    return success_response(
        data={
            **file_info(artifact),
            "resourceUrl": "/v1/workbench/applications/"
            + quote(key, safe="")
            + "/capabilities/files/"
            + quote(body.fileId, safe="")
            + "?"
            + urlencode(
                {
                    "revision": body.revision,
                    **({"instanceId": body.instanceId} if body.instanceId else {}),
                }
            ),
        }
    )


@router.get("/files/{file_id}")
def download(
    key: str,
    file_id: str,
    revision: str,
    instanceId: str | None = None,
    approvalId: str = Header(..., alias="X-Workbench-Approval"),
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    _, value, _, effective = grants.authorized(
        db, user.user_id, key, revision, approvalId, instance_id=instanceId
    )
    if not effective & {"file.pick", "file.save"}:
        external.fail(403, "capability_denied")
    if file_id not in value["files"]:
        external.fail(403, "file_not_selected")
    artifact = grants.readable_file(db, user.user_id, file_id)
    if artifact.size_bytes > 50 * 1024 * 1024:
        external.fail(413, "file_too_large")
    import os
    import tempfile
    from pathlib import Path

    from core.storage import get_storage
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    root = Path(os.environ.get("STORAGE_PATH", "storage")) / ".workbench-resources"
    root.mkdir(parents=True, exist_ok=True)
    handle, path = tempfile.mkstemp(dir=root)
    os.close(handle)
    try:
        get_storage().download(artifact.storage_key, path)
        if Path(path).stat().st_size > 50 * 1024 * 1024:
            external.fail(413, "file_too_large")
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    try:
        grants.authorized(db, user.user_id, key, revision, approvalId, instance_id=instanceId)
        grants.readable_file(db, user.user_id, file_id)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(Path(path).unlink, missing_ok=True),
    )


@router.post("/files/save")
async def save(
    key: str,
    approvalId: str = Form(...),
    revision: str = Form(...),
    instanceId: str | None = Form(None),
    file: UploadFile = File(...),
    user: UserContext = Depends(host_identity),
    db: Session = Depends(get_db),
):
    lease, _, _ = grants.reserve(
        db, user.user_id, key, revision, approvalId, "file.save", instanceId
    )
    try:
        content = await asyncio.wait_for(file.read(50 * 1024 * 1024 + 1), timeout=30)
        if not content or len(content) > 50 * 1024 * 1024:
            external.fail(413, "file_size_invalid")
        filename = file.filename or ""
        if not filename or len(filename) > 200 or "/" in filename or "\\" in filename:
            external.fail(422, "invalid_filename")
        runtime(db, user.user_id)
        grants.authorized(db, user.user_id, key, revision, approvalId, "file.save", instanceId)
        from core.services.artifact_service import store_bytes_as_artifact

        artifact = store_bytes_as_artifact(
            db,
            user_id=user.user_id,
            content=content,
            filename=filename,
            mime_type=file.content_type,
            source="workbench",
        )
        return success_response(data=file_info(artifact))
    finally:
        grants.release(db, approvalId, lease)

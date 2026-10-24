"""File manager API over the same authorization manifest used by sandboxes."""
import mimetypes
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from api.routes.v1.file_text import TextSaveRequest
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.files.spaces.access import resolve_grants
from core.files.spaces.mounts import mount_revision
from core.files.spaces.files import MAX_TRANSFER_BYTES, SpaceFiles
from core.files.storage import get_storage
from core.infra.byte_range import byte_range_response

router = APIRouter(prefix="/v1/myspace", tags=["Space Files"])

class PathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space: str
    path: str

class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space: str
    source: str
    destination: str

def service(user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    return SpaceFiles(db, str(user.user_id), get_storage())

@router.get("/mounts")
def mounts(user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)):
    grants = resolve_grants(db, str(user.user_id))
    return success_response(data={"items": [g.public() for g in grants],
        "revision": mount_revision(grants)})

@router.get("/entries")
def entries(space: str, path: str = "", cursor: str | None = None,
    limit: int = Query(200, ge=1, le=1000), keyword: str = "", files: SpaceFiles = Depends(service)):
    return success_response(data=files.list(space, path, cursor=cursor, limit=limit, keyword=keyword))

@router.get("/content")
def download(request: Request, space: str, path: str, inline: bool = False,
    files: SpaceFiles = Depends(service)):
    data, info = files.read(space, path)
    mime = mimetypes.guess_type(info.name)[0] or "application/octet-stream"
    safe_inline = inline and (mime.startswith(("image/", "audio/", "video/", "text/")) or mime == "application/pdf")
    # Range lets the canvas audio/video player seek without refetching the whole file.
    return byte_range_response(data, request.headers.get("range"),
        media_type=mime if safe_inline else "application/octet-stream",
        headers={"Content-Disposition": ("inline" if safe_inline else "attachment") + "; filename*=UTF-8''" + quote(info.name, safe=""),
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

@router.put("/content")
async def upload(request: Request, space: str, path: str, files: SpaceFiles = Depends(service)):
    files.grant(space, write=True)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_TRANSFER_BYTES:
            raise HTTPException(413, "File exceeds transfer limit")
        data.extend(chunk)
    from starlette.concurrency import run_in_threadpool
    result = await run_in_threadpool(files.write, space, path, bytes(data))
    return success_response(data=result)

@router.post("/directories")
def mkdir(body: PathBody, files: SpaceFiles = Depends(service)):
    return success_response(data=files.mkdir(body.space, body.path))

@router.post("/moves")
def move(body: MoveBody, files: SpaceFiles = Depends(service)):
    return success_response(data=files.move(body.space, body.source, body.destination))

@router.delete("/entries")
def delete(space: str, path: str, directory: bool = False, files: SpaceFiles = Depends(service)):
    files.delete(space, path, directory=directory)
    return success_response(data={"path": path})


@router.post("/references")
def reference(body: PathBody, files: SpaceFiles = Depends(service)):
    from core.files.artifacts.store import save_artifact_bytes
    data, info = files.read(body.space, body.path)
    mime = mimetypes.guess_type(info.name)[0] or "application/octet-stream"
    extension = info.name.rpartition(".")[2] if "." in info.name else ""
    item = save_artifact_bytes(content=data, name=info.name, mime_type=mime,
        extension=extension, metadata={"user_id": files.actor, "source": "space_reference"})
    return success_response(data={
        "file_id": item["file_id"], "name": info.name, "origin": "cloud",
        "download_url": "/files/" + item["file_id"], "mime_type": mime,
        "type": "image" if mime.startswith("image/") else "document",
    })


@router.get("/text")
def read_text(space: str, path: str, response: Response, files: SpaceFiles = Depends(service)):
    from core.files.spaces.text import SpaceText
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=SpaceText(files).read(space, path))


@router.put("/text")
def save_text(body: TextSaveRequest, space: str, path: str, files: SpaceFiles = Depends(service)):
    from core.files.spaces.text import SpaceText
    return success_response(data=SpaceText(files).save(space, path, body.content, body.revision))

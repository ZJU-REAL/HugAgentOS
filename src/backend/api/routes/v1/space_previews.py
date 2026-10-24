"""Authorized document and archive previews for mounted files."""
from contextlib import contextmanager
import mimetypes
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from core.files.content.archive_browser import ArchiveError, inspect_archive, read_archive_member
from core.infra.byte_range import byte_range_response
from core.infra.responses import success_response
from core.files.spaces.files import SpaceFiles
from api.routes.v1.space_files import service

router = APIRouter(prefix="/v1/myspace/content")


@contextmanager
def source(files, space, path):
    data, info = files.read(space, path)
    with tempfile.TemporaryDirectory(prefix="space-preview-") as directory:
        local = Path(directory) / "source"
        local.write_bytes(data)
        try:
            yield str(local), info.name
        except ArchiveError as exc:
            raise HTTPException(exc.status, exc.code) from exc


def office_response(data, filename, background_tasks):
    from api.routes.files import render_office_file
    directory = tempfile.mkdtemp(prefix="space-office-")
    try:
        local = Path(directory) / ("source" + PurePosixPath(filename).suffix)
        local.write_bytes(data)
        result = render_office_file(str(local), "space-preview", background_tasks, filename=filename)
        background_tasks.add_task(shutil.rmtree, directory, ignore_errors=True)
        return result
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@router.get("/preview")
def preview(background_tasks: BackgroundTasks, space: str, path: str,
    format: str = "pdf", files: SpaceFiles = Depends(service)):
    if format != "pdf":
        raise HTTPException(400, "Unsupported preview format")
    data, info = files.read(space, path)
    return office_response(data, info.name, background_tasks)


@router.get("/archive")
def archive(space: str, path: str, response: Response, files: SpaceFiles = Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    with source(files, space, path) as (local, name):
        return success_response(inspect_archive(local, name))


@router.get("/archive/member")
def member(request: Request, space: str, path: str, member: str, inline: bool = False,
           files: SpaceFiles = Depends(service)):
    with source(files, space, path) as (local, name):
        data = read_archive_member(local, name, member)
    filename = PurePosixPath(member).name
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime in ("text/html", "image/svg+xml"):
        mime = "text/plain"
    safe = inline and (mime.startswith(("image/", "audio/", "video/", "text/")) or mime == "application/pdf")
    return byte_range_response(data, request.headers.get("range"), media_type=mime, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Content-Disposition": ("inline" if safe else "attachment") + "; filename*=UTF-8''" + quote(filename, safe=""),
    })


@router.get("/archive/member/preview")
def member_preview(background_tasks: BackgroundTasks, space: str, path: str, member: str,
                   files: SpaceFiles = Depends(service)):
    with source(files, space, path) as (local, name):
        data = read_archive_member(local, name, member)
    return office_response(data, PurePosixPath(member).name, background_tasks)

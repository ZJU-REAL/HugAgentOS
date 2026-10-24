"""Authenticated archive listing and member access mounted under /files."""

from contextlib import contextmanager
import mimetypes
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.files.content.archive_browser import ArchiveError, MAX_ARCHIVE_BYTES, inspect_archive, read_archive_member
from core.db.engine import get_db
from core.infra.byte_range import byte_range_response, is_continuation_range
from core.infra.exceptions import StorageError
from core.infra.responses import success_response
from core.files.storage import get_storage
from core.files.storage.local import LocalStorageBackend

router = APIRouter()


@contextmanager
def _source(file_id, user, db, *, audit: bool = True):
    from api.routes.files import _authorize_access, _load_artifact_item, _record_audit

    item = _load_artifact_item(file_id, db)
    if (item.get("metadata") or {}).get("deleted"):
        raise HTTPException(404, detail="archive_missing")
    _authorize_access(
        item=item,
        file_id=file_id,
        user=user,
        db=db,
        denied_action="file.archive.denied",
    )
    if item.get("size", 0) > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, detail="archive_too_large")
    try:
        with tempfile.TemporaryDirectory(prefix="archive-source-") as folder:
            local = item.get("path")
            if local and Path(local).is_file():
                source = str(local)
            else:
                storage = get_storage()
                if isinstance(storage, LocalStorageBackend):
                    source = str(storage.resolve_file_path(item["storage_key"]))
                else:
                    source = str(Path(folder) / "source")
                    storage.download(item["storage_key"], source)
            yield source, str(item.get("name", "")), item
    except ArchiveError as exc:
        raise HTTPException(exc.status, detail=exc.code) from exc
    except (OSError, KeyError, StorageError) as exc:
        raise HTTPException(503, detail="archive_read_failed") from exc
    if not audit:
        return
    _record_audit(
        user=user,
        db=db,
        action="file.archive.read",
        file_id=file_id,
        item=item,
        status="success",
    )


@router.get("/{file_id}/archive", summary="浏览压缩包目录")
def list_archive(
    file_id: str,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    with _source(file_id, user, db) as (source, name, _):
        return success_response(inspect_archive(source, name))


@router.get("/{file_id}/archive/member", summary="读取压缩包内文件")
def archive_member(
    request: Request,
    file_id: str,
    path: str = Query(min_length=1, max_length=1024),
    inline: bool = False,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    range_header = request.headers.get("range")
    # A player's mid-file seeks re-read the member; audit only the first read.
    with _source(file_id, user, db, audit=not is_continuation_range(range_header)) as (source, name, _):
        data = read_archive_member(source, name, path)
    filename = PurePosixPath(path).name
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Active documents must not run in the authenticated application's origin.
    safe_inline = inline and (
        mime == "application/pdf"
        or (
            mime.startswith(("image/", "audio/", "video/", "text/"))
            and mime not in ("image/svg+xml", "text/html")
        )
    )
    if mime in ("text/html", "image/svg+xml"):
        mime = "text/plain"
        safe_inline = inline
    return byte_range_response(
        data,
        range_header,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": f"{'inline' if safe_inline else 'attachment'}; filename*=UTF-8''{quote(filename)}",
        },
    )


@router.get("/{file_id}/archive/member/preview", summary="预览压缩包内 Office 文件")
def preview_archive_member(
    file_id: str,
    background_tasks: BackgroundTasks,
    path: str = Query(min_length=1, max_length=1024),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from api.routes.files import _cleanup_path, render_office_file

    with _source(file_id, user, db) as (source, name, _):
        data = read_archive_member(source, name, path)
    filename = PurePosixPath(path).name
    folder = tempfile.mkdtemp(prefix="archive-member-")
    try:
        source = Path(folder) / ("member" + PurePosixPath(filename).suffix)
        source.write_bytes(data)
        response = render_office_file(
            str(source),
            file_id,
            background_tasks,
            filename=filename,
        )
    finally:
        _cleanup_path(folder)
    return response

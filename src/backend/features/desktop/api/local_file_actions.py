"""Authenticated native actions; never accepts a client-supplied filesystem path."""

from typing import Literal
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db

from api.routes.file_location import router as location_router

router = APIRouter()
router.include_router(location_router)


class FileOpenRequest(BaseModel):
    action: Literal["open", "reveal"] = "open"
    application: str | None = None


@router.get("/{file_id}/applications")
def applications(
    file_id: str, user: UserContext = Depends(require_auth()), db: Session = Depends(get_db),
    include_icons: bool = True,
):
    from api.routes.files import local_file_location
    from features.desktop.local.file_actions import file_applications

    location = local_file_location(file_id, user, db)
    try:
        return {**file_applications(location["path"], include_icons=include_icons), "path": location["path"]}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "无法读取关联应用，请重试") from exc


@router.post("/{file_id}/open")
def open_local_file(
    file_id: str, request: FileOpenRequest,
    user: UserContext = Depends(require_auth()), db: Session = Depends(get_db),
):
    from api.routes.files import local_file_location
    from features.desktop.local.file_actions import open_file

    location = local_file_location(file_id, user, db)
    try:
        open_file(location["path"], action=request.action, application=request.application)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "无法打开文件，请重试") from exc
    return {"ok": True}

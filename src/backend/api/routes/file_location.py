"""Authenticated logical file location for preview toolbars."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.db.models import Artifact
from core.files.artifacts.location import artifact_location

router = APIRouter()


@router.get("/{file_id}/location")
def file_location(
    file_id: str, user: UserContext = Depends(require_auth()), db: Session = Depends(get_db)
):
    from api.routes.files import _load_artifact_item, _authorize_access

    item = _load_artifact_item(file_id, db)
    if (item.get("metadata") or {}).get("deleted"):
        raise HTTPException(404, "文件已移动或删除")
    _authorize_access(
        item=item, file_id=file_id, user=user, db=db, denied_action="file.location.denied"
    )
    artifact = db.get(Artifact, file_id)
    return artifact_location(db, artifact)

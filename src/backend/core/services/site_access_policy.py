"""Single-owner site access policy for Community Edition."""

from typing import Optional

from core.infra.exceptions import BadRequestError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session


class SiteUpdateScopeFields(BaseModel):
    visibility: Optional[str] = Field(
        None,
        description="可见性：public / private",
        pattern="^(public|private)$",
    )


class SitePublishScopeFields(BaseModel):
    visibility: str = Field(
        "public",
        description="可见性：public / private",
        pattern="^(public|private)$",
    )


def resolve_site_scope(
    _db: Session,
    _user_id: str,
    visibility: str,
    _scope_id: Optional[str] = None,
) -> None:
    if visibility not in ("public", "private"):
        raise BadRequestError("visibility 仅支持 public / private")
    return None


def site_scope_write_fields(_scope_id: Optional[str]) -> dict:
    return {}


def site_scope_ref(fields) -> None:
    return None


def serialize_site_scope(site) -> dict:
    return {"visibility": site.visibility}


def can_view_site(_db: Session, site, viewer_user_id: Optional[str]) -> bool:
    if site.visibility == "public":
        return True
    return bool(viewer_user_id and viewer_user_id == site.user_id)

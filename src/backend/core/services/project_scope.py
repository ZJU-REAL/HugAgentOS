"""Personal project scope for Community Edition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProjectScope:
    project_id: str
    kind: str
    root_folder_id: str
    folder_name: str

    @property
    def is_personal(self) -> bool:
        return True

    @property
    def is_team(self) -> bool:
        return False


def project_scope_from_context(ctx: dict[str, Any]) -> Optional[ProjectScope]:
    if not ctx.get("project_id") or ctx.get("project_folder_kind") != "personal":
        return None
    folder_id = ctx.get("project_folder_id")
    if not folder_id:
        return None
    return ProjectScope(
        project_id=str(ctx["project_id"]),
        kind="personal",
        root_folder_id=str(folder_id),
        folder_name=str(ctx.get("project_folder_name") or ""),
    )


def project_scope_from_chat_id(db: "Session", chat_id: Optional[str]) -> Optional[ProjectScope]:
    if not chat_id:
        return None
    from core.db.models import ChatSession, Project, UserFolder

    chat = (
        db.query(ChatSession)
        .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
        .first()
    )
    if chat is None or not chat.project_id:
        return None
    project = (
        db.query(Project)
        .filter(
            Project.project_id == chat.project_id,
            Project.kind == "personal",
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None or not project.linked_folder_id:
        return None
    row = db.query(UserFolder.name).filter(UserFolder.folder_id == project.linked_folder_id).first()
    return ProjectScope(
        project_id=project.project_id,
        kind="personal",
        root_folder_id=project.linked_folder_id,
        folder_name=row[0] if row else "",
    )


def build_project_ctx(db: "Session", project_id: Optional[str]) -> Optional[dict]:
    if not project_id:
        return None
    from core.db.models import Project, UserFolder
    from core.services.project_file_service import ProjectFileService

    project = (
        db.query(Project)
        .filter(
            Project.project_id == project_id,
            Project.kind == "personal",
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None:
        return None
    folder_name = None
    if project.linked_folder_id:
        row = (
            db.query(UserFolder.name)
            .filter(UserFolder.folder_id == project.linked_folder_id)
            .first()
        )
        folder_name = row[0] if row else None
    try:
        files = ProjectFileService(db).list_files(project)
    except Exception:
        files = []
    return {
        "project_id": project_id,
        "project_name": project.name,
        "project_instructions": (project.instructions or "").strip() or None,
        "project_folder_name": folder_name,
        "project_folder_kind": "personal" if project.linked_folder_id else None,
        "project_folder_id": project.linked_folder_id,
        "project_files": files,
        "memory_scope_user_id": None,
        "_memory_enabled": bool((project.extra_data or {}).get("memory_enabled", True)),
        "_memory_write_enabled": bool((project.extra_data or {}).get("memory_write_enabled", True)),
    }


def build_project_ctx_from_chat_id(db: "Session", chat_id: Optional[str]) -> Optional[dict]:
    if not chat_id:
        return None
    from core.db.models import ChatSession

    chat = (
        db.query(ChatSession)
        .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
        .first()
    )
    return build_project_ctx(db, chat.project_id) if chat and chat.project_id else None


def project_memory_policy(
    db: "Session", project_id: str, user_id: str
) -> Optional[tuple[bool, str]]:
    from core.db.models import Project

    project = (
        db.query(Project)
        .filter(
            Project.project_id == project_id,
            Project.kind == "personal",
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None:
        return None
    return bool((project.extra_data or {}).get("memory_enabled", True)), user_id


def edition_project_context_keys() -> tuple[str, ...]:
    return ()


__all__ = [
    "ProjectScope",
    "build_project_ctx",
    "build_project_ctx_from_chat_id",
    "edition_project_context_keys",
    "project_memory_policy",
    "project_scope_from_chat_id",
    "project_scope_from_context",
]

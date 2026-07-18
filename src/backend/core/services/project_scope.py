"""ProjectScope — the unified type for project-chat scope.

Constructed once at the chats.py entry point and passed **explicitly** down the call chain to
everywhere that needs it (agent_factory → register_* tool closures → myspace_vfs.py helpers →
_persist_artifacts).

Compared with the previous ContextVar model:
- No set/reset timing window; a missing scope is a parameter/type-level issue and no longer
  silently falls back to the personal root (which once caused a bug where AI-generated files
  under a team project leaked into the personal MySpace root).
- The scope follows the call chain rather than being thread-local, so it remains valid across
  async generator finally boundaries.
- In tests just construct one directly; no need to monkeypatch a contextvar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProjectScope:
    """Immutable description of a project-chat scope.

    Attributes:
        project_id: Project ID.
        kind: ``"personal"`` or ``"team"`` — decides whether to use UserFolder or TeamFolder.
        root_folder_id: Linked folder ID (personal=UserFolder.folder_id;
            team=TeamFolder.folder_id). All /myspace paths are confined to this subtree.
        folder_name: Name of the linked folder. Used to auto-complete a "bare relative path"
            like ``/myspace/foo.txt`` into ``/myspace/<folder_name>/foo.txt``.
        team_id: Only valid for team kind; points to the team ID so artifact queries use
            team_id rather than user_id (visible across members).
    """

    project_id: str
    kind: Literal["personal", "team"]
    root_folder_id: str
    folder_name: str
    team_id: Optional[str] = None

    @property
    def is_team(self) -> bool:
        return self.kind == "team"

    @property
    def is_personal(self) -> bool:
        return self.kind == "personal"


def project_scope_from_context(ctx: dict[str, Any]) -> Optional[ProjectScope]:
    """Extract a ProjectScope from the workflow context dict built by chats.py.

    Missing any of project_id / project_folder_id / project_folder_kind → return None
    (meaning a non-project chat; never construct a dummy scope, so downstream code can
    reliably check for None).

    team kind must carry project_team_id, otherwise also return None (defensive: avoids
    constructing a team scope missing team_id that would misdirect downstream queries).
    """
    if not ctx.get("project_id"):
        return None
    folder_id = ctx.get("project_folder_id")
    if not folder_id:
        return None
    kind = ctx.get("project_folder_kind")
    if kind not in ("personal", "team"):
        return None
    team_id: Optional[str] = None
    if kind == "team":
        team_id = ctx.get("project_team_id") or None
        if not team_id:
            return None
    return ProjectScope(
        project_id=str(ctx["project_id"]),
        kind=kind,  # type: ignore[arg-type]
        root_folder_id=str(folder_id),
        folder_name=str(ctx.get("project_folder_name") or ""),
        team_id=team_id,
    )


def project_scope_from_chat_id(
    db: "Session", chat_id: Optional[str]
) -> Optional[ProjectScope]:
    """Look up the owning project by chat_id and construct a ProjectScope.

    For entry points that don't hold the workflow context dict (e.g. the plan-execute
    background worker, cancel/cleanup paths). Returns None when the chat isn't in a project /
    the chat doesn't exist / the project's linked folder is missing.

    Guarantees field semantics consistent with :func:`project_scope_from_context`:
    team kind must carry team_id; none may be missing.
    """
    if not chat_id:
        return None
    # Deferred import to avoid a top-level circular dependency (models depends on db.engine ↔ services)
    from core.db.models import ChatSession, Project, TeamFolder, UserFolder

    chat = (
        db.query(ChatSession)
        .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
        .first()
    )
    if chat is None or not chat.project_id:
        return None
    project = (
        db.query(Project)
        .filter(Project.project_id == chat.project_id, Project.deleted_at.is_(None))
        .first()
    )
    if project is None:
        return None
    if project.kind == "personal" and project.linked_folder_id:
        row = (
            db.query(UserFolder.name)
            .filter(UserFolder.folder_id == project.linked_folder_id)
            .first()
        )
        return ProjectScope(
            project_id=project.project_id,
            kind="personal",
            root_folder_id=project.linked_folder_id,
            folder_name=row[0] if row else "",
            team_id=None,
        )
    if project.kind == "team" and project.linked_team_folder_id and project.team_id:
        row = (
            db.query(TeamFolder.name)
            .filter(TeamFolder.folder_id == project.linked_team_folder_id)
            .first()
        )
        return ProjectScope(
            project_id=project.project_id,
            kind="team",
            root_folder_id=project.linked_team_folder_id,
            folder_name=row[0] if row else "",
            team_id=project.team_id,
        )
    return None


def build_project_ctx(db: "Session", project_id: Optional[str]) -> Optional[dict]:
    """Build the project context dict (for injecting into the agent system prompt + resolving ProjectScope).

    Returned fields align with ``routing.workflow._PROJECT_CTX_KEYS``; returns None when
    ``project_id`` is missing or the project doesn't exist. Reused by entry points that
    **don't go through ``chats._build_ctx``** (e.g. plan mode), avoiding duplication of that
    project metadata query logic.
    """
    if not project_id:
        return None
    # Deferred import to avoid a top-level circular dependency.
    from core.db.models import Project, TeamFolder, UserFolder
    from core.services.project_file_service import ProjectFileService

    project = (
        db.query(Project)
        .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if project is None:
        return None

    folder_name: Optional[str] = None
    folder_kind: Optional[str] = None
    folder_id: Optional[str] = None
    team_id: Optional[str] = None
    if project.kind == "personal" and project.linked_folder_id:
        row = (
            db.query(UserFolder.name)
            .filter(UserFolder.folder_id == project.linked_folder_id)
            .first()
        )
        folder_name = row[0] if row else None
        folder_kind = "personal"
        folder_id = project.linked_folder_id
    elif project.kind == "team" and project.linked_team_folder_id:
        row = (
            db.query(TeamFolder.name)
            .filter(TeamFolder.folder_id == project.linked_team_folder_id)
            .first()
        )
        folder_name = row[0] if row else None
        folder_kind = "team"
        folder_id = project.linked_team_folder_id
        team_id = project.team_id

    try:
        project_files = ProjectFileService(db).list_files(project)
    except Exception:
        project_files = []

    return {
        "project_id": project_id,
        "project_name": project.name,
        "project_instructions": (project.instructions or "").strip() or None,
        "project_folder_name": folder_name,
        "project_folder_kind": folder_kind,
        "project_folder_id": folder_id,
        "project_team_id": team_id,
        "project_files": project_files,
    }


def build_project_ctx_from_chat_id(db: "Session", chat_id: Optional[str]) -> Optional[dict]:
    """Reverse lookup ``chat_id`` → ``ChatSession.project_id`` → :func:`build_project_ctx`.

    The plan-mode background worker holds no workflow context dict, only a chat_id, so it uses
    this reverse-lookup path to build the project context (same source as
    :func:`project_scope_from_chat_id`).
    """
    if not chat_id:
        return None
    from core.db.models import ChatSession

    chat = (
        db.query(ChatSession)
        .filter(ChatSession.chat_id == chat_id, ChatSession.deleted_at.is_(None))
        .first()
    )
    if chat is None or not chat.project_id:
        return None
    return build_project_ctx(db, chat.project_id)


__all__ = [
    "ProjectScope",
    "project_scope_from_context",
    "project_scope_from_chat_id",
    "build_project_ctx",
    "build_project_ctx_from_chat_id",
]

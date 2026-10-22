"""Personal-project response serialization."""
from __future__ import annotations
from typing import Any, Dict, Optional
from core.db.models import Project

def summarize(
    project: Project,
    *,
    favorite: bool,
    folder_name: Optional[str],
    file_count: int,
    chat_count: int,
) -> Dict[str, Any]:
    extra = project.extra_data or {}
    local = extra.get("local") or {}
    return {
        "project_id": project.project_id,
        "name": project.name,
        "description": project.description or "",
        "kind": project.kind,
        "is_local": project.kind == "local",
        "local_path": local.get("path"),
        "local_slug": local.get("slug"),
        "local_machine": local.get("machine"),
        "owner_user_id": project.owner_user_id,
        "linked_folder_id": project.linked_folder_id,
        "folder_name": folder_name,
        "instructions": project.instructions or "",
        "icon_color": project.icon_color,
        "pinned": bool(project.pinned),
        "favorite": favorite,
        "memory_enabled": bool(extra.get("memory_enabled", True)),
        "memory_write_enabled": bool(extra.get("memory_write_enabled", True)),
        "permission": "admin",
        "is_owner": True,
        "file_count": file_count,
        "chat_count": chat_count,
        "metadata": extra,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "last_activity_at": (
            project.last_activity_at.isoformat() if project.last_activity_at else None
        ),
    }

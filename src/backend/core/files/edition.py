"""Personal ownership, mounts and artifact policy for Community Edition."""
from __future__ import annotations

def project_directory(scope):
    return ""

def project_directory_by_id(project_id):
    return ""

def workspace_instructions():
    return ""

def is_organization_path(scope, actor, path):
    return False

def resolve_alias(path):
    return None

def resolve_project_path(path, user_id, root, scope):
    return None

def is_shared_scope(scope):
    return False

def context_directory(context):
    return ""

def source_directory(project):
    return ""

def folder_scope_label(kind):
    return "我的空间"

def command_instructions(scope):
    return ""

from typing import Any, Optional

def artifact_list_scope() -> str:
    return "personal"

def extend_artifact_item(artifact, item: dict) -> dict:
    return item

def personal_artifact_create_fields() -> dict[str, None]:
    return {}

def artifact_scope_fields(scope: Any) -> dict[str, Optional[str]]:
    return {
        "user_folder_id": (
            scope.root_folder_id or None if scope is not None and scope.is_personal else None
        )
    }

def artifact_scope_folder_id(scope: Any, artifact: Any) -> Optional[str]:
    return artifact.user_folder_id

def can_access_artifact(db: Any, user_id: str, artifact: Any) -> bool:
    return str(artifact.user_id) == str(user_id)

def artifact_access_metadata(artifact: Any) -> dict[str, Optional[str]]:
    return {"owner_id": artifact.user_id}

def can_access_artifact_metadata(db: Any, user_id: str, metadata: dict[str, Any]) -> bool:
    owner_id = metadata.get("owner_id") or metadata.get("user_id")
    return owner_id is None or str(owner_id) == str(user_id)

def upload_target_matches(artifact: Any, scope: str, folder_id: Optional[str]) -> bool:
    return scope == "personal" and artifact.user_folder_id == folder_id

workspace_mount_names = ("myspace", "skills")

def team_grants(db, actor):
    return ()

def session_entrypoint(entrypoint, user_id):
    return list(entrypoint)

def project_location(db, project):
    return None

def matches_scope(project, scope):
    return scope.kind == project.kind and scope.root_folder_id == project.linked_folder_id

def owner_space(column, owner):
    raise ValueError("Unsupported directory owner")

def revoke_computers(actor):
    return None


def artifact_location_scope(artifact):
    from core.db.models import UserFolder
    return "personal", artifact.user_folder_id, UserFolder, "user_id", artifact.user_id


def artifact_location_root(db, artifact):
    return None

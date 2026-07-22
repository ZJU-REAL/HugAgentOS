"""Community artifact lists are always personal."""

from typing import Any, Optional


def artifact_list_scope() -> str:
    return "personal"


def extend_artifact_item(artifact, item: dict) -> dict:
    return item


def personal_artifact_predicates(artifact_model: Any) -> list[Any]:
    return []


def is_personal_artifact(artifact: Any) -> bool:
    return True


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


__all__ = [
    "artifact_access_metadata",
    "artifact_list_scope",
    "artifact_scope_fields",
    "artifact_scope_folder_id",
    "can_access_artifact",
    "can_access_artifact_metadata",
    "extend_artifact_item",
    "is_personal_artifact",
    "personal_artifact_create_fields",
    "personal_artifact_predicates",
]

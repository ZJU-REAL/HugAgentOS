"""Community Edition has no organization-scoped MySpace tools."""

from typing import Any, Optional


def project_subtree_folder_ids(db: Any, scope: Any) -> None:
    return None


def list_organization_project_files(
    db: Any,
    *,
    scope: Any,
    folder_id: str,
    mime_prefix: Optional[str],
    keyword: str,
    limit: int,
) -> None:
    return None


def find_organization_project_artifact(db: Any, *, scope: Any, reference: str) -> tuple[bool, Any]:
    return False, None


def register_organization_tools(toolkit: Any, user_id: str) -> None:
    return None


__all__ = [
    "find_organization_project_artifact",
    "list_organization_project_files",
    "project_subtree_folder_ids",
    "register_organization_tools",
]

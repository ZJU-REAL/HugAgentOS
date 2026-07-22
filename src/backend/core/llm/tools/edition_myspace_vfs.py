"""Community Edition has no organization-scoped MySpace storage."""

from typing import Any, Optional


def organization_scope_id(scope: Any) -> Optional[str]:
    return None


def resolve_organization_folder(
    db: Any, scope: Any, folder_names: list[str], *, create: bool = False
) -> None:
    return None


def resolve_organization_artifact(
    db: Any, scope: Any, folder_id: Optional[str], filename: str
) -> None:
    return None


def iter_organization_tree(db: Any, scope: Any, root_folder_id: Optional[str]) -> None:
    return None


def organization_subtree_folder_ids(db: Any, scope: Any, root_folder_id: str) -> None:
    return None


def organization_cache_file(scope: Any, rel: str) -> None:
    return None


def organization_mutation_blocked(scope: Any) -> bool:
    return False


__all__ = [
    "iter_organization_tree",
    "organization_cache_file",
    "organization_mutation_blocked",
    "organization_scope_id",
    "organization_subtree_folder_ids",
    "resolve_organization_artifact",
    "resolve_organization_folder",
]

"""Host-owned package interface validation, presentation and enablement."""
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass
class PackageViews:
    project: Callable | None = None
    normalize: Callable | None = None
    find: Callable | None = None
    enabled: Callable | None = None
    workspace_only: Callable | None = None
    set_enabled: Callable | None = None


@lru_cache(maxsize=1)
def package_views() -> PackageViews:
    return PackageViews()


def register_package_view(project, *, normalize, find, enabled, workspace_only, set_enabled) -> None:
    services = package_views()
    services.project = project
    services.normalize = normalize
    services.find = find
    services.enabled = enabled
    services.workspace_only = workspace_only
    services.set_enabled = set_enabled


def public_package_view(declaration: Any, *, slug: str) -> dict[str, Any]:
    project = package_views().project
    return project(declaration, slug=slug) if project is not None else {}


def normalize_ui(declaration):
    normalize = package_views().normalize
    if normalize is None:
        raise RuntimeError("Package interface validator is not registered")
    return normalize(declaration)


def find_module(stored, module_id):
    find = package_views().find
    if find is None:
        raise RuntimeError("Package interface resolver is not registered")
    return find(stored, module_id)


def enabled(db, row, user_id):
    predicate = package_views().enabled
    return bool(predicate and predicate(db, row, user_id))


def workspace_only(row):
    predicate = package_views().workspace_only
    return bool(predicate and predicate(row))


def set_enabled(db, row, value, user_id=None):
    update = package_views().set_enabled
    if update is None:
        raise RuntimeError("Package interface enablement policy is not registered")
    return update(db, row, value, user_id=user_id)

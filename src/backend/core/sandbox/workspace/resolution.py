"""Select desktop or container path rules for the registered file tools."""

from typing import Optional
from core.sandbox.workspace.paths import WORKSPACE as WORKSPACE_ROOT

SCRATCH_ROOT = f"{WORKSPACE_ROOT}/scratch"
MYSPACE_LOGICAL = "/myspace"


def path_rules():
    from core.harness.registry.evaluation import current_path_rules
    restricted_paths = current_path_rules()
    if restricted_paths is not None:
        return restricted_paths
    from core.config.local_mode import local_mode_enabled

    if local_mode_enabled():
        import core.sandbox.workspace.desktop as desktop_paths

        return desktop_paths
    import core.sandbox.workspace.container as sandbox_paths

    return sandbox_paths


def workspace_directory(session_id: str | None, *, scope=None) -> str:
    rules = path_rules()
    import core.sandbox.workspace.desktop as desktop_paths

    if rules is desktop_paths:
        return rules.workspace_directory(WORKSPACE_ROOT, session_id, scope=scope)
    return rules.workspace_directory(WORKSPACE_ROOT, session_id)


def validate_workspace_path(path: str) -> Optional[str]:
    return path_rules().validate_path(path, WORKSPACE_ROOT)


def to_physical_path(
    path: str, user_id: Optional[str], *, session_id: Optional[str] = None, scope=None
) -> str:
    rules = path_rules()
    import core.sandbox.workspace.desktop as desktop_paths

    if rules is desktop_paths:
        return rules.resolve_path(path, WORKSPACE_ROOT, session_id, user_id, scope=scope)
    from core.files.edition import resolve_project_path

    resolved = resolve_project_path(path, user_id, WORKSPACE_ROOT, scope)
    if resolved is not None:
        return resolved
    return rules.resolve_path(path, WORKSPACE_ROOT, session_id, user_id)


def validate_project_scope_path(path: str, project_folder_name: Optional[str]) -> Optional[str]:
    return path_rules().validate_project_scope_path(path, project_folder_name)


def is_myspace_physical(physical_path: str, user_id: Optional[str]) -> bool:
    return path_rules().is_myspace_physical(physical_path, user_id, WORKSPACE_ROOT)


def parent_dir(path: str) -> str:
    """Return the parent directory portion of ``path`` (no trailing slash)."""
    if "/" not in path:
        return WORKSPACE_ROOT
    return path.rsplit("/", 1)[0] or WORKSPACE_ROOT


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def quote_shell_path(path: str) -> str:
    return path_rules().quote_shell_path(path)


def space_metadata(physical_path: str, user_id: Optional[str]) -> dict:
    """Describe durable mounted space independently of its personal/shared owner."""
    if is_myspace_physical(physical_path, user_id):
        kind = "personal"
    else:
        from core.files.edition import is_organization_path

        kind = "team" if is_organization_path(None, user_id, physical_path) else "temporary"
    return {"persistent": kind != "temporary", "space_type": kind}

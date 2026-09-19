"""Select desktop or container path rules for the registered file tools."""
from typing import Optional
from core.sandbox._common import WORKSPACE as WORKSPACE_ROOT

SCRATCH_ROOT = f"{WORKSPACE_ROOT}/scratch"
MYSPACE_LOGICAL = "/myspace"


def path_rules():
    from core.config.local_mode import local_mode_enabled
    if local_mode_enabled():
        from core.sandbox import desktop_paths
        return desktop_paths
    from core.sandbox import sandbox_paths
    return sandbox_paths


def workspace_directory(session_id: str | None) -> str:
    return path_rules().workspace_directory(WORKSPACE_ROOT, session_id)


def validate_workspace_path(path: str) -> Optional[str]:
    return path_rules().validate_path(path, WORKSPACE_ROOT)


def to_physical_path(path: str, user_id: Optional[str], *, session_id: Optional[str] = None) -> str:
    return path_rules().resolve_path(path, WORKSPACE_ROOT, session_id, user_id)


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

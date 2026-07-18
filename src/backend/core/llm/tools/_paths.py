"""Path resolution helpers shared by Read / Edit / Write / Glob / Grep.

Design principles (B+in-place+immediate+/scratch):

- The "persistent filesystem" the model sees is called ``/myspace/`` — a
  logical path. This is the user's "My Space".
- What actually exists in the sandbox is ``/workspace/myspace/{user_id}/`` —
  the physical path. We create a ``/myspace`` symlink in the sandbox pointing
  at it, so bash commands can use the short path directly too.
- The "temporary compute area" the model sees is called
  ``/workspace/scratch/`` — never reverse-synced to myspace; good for
  intermediate scripts, debug output, pip extraction dirs and the like.
- The old long path ``/workspace/myspace/{user_id}/`` is still supported
  (backward compatibility).

Valid path shapes accepted at tool entry:
  /myspace                            (maps to the myspace root)
  /myspace/<path>                     (maps to /workspace/myspace/<uid>/<path>)
  /workspace/myspace/<uid>/<path>     (already physical, used as-is)
  /workspace/scratch/<path>           (temp area, not synced)
  /workspace/<path>                   (other sandbox locations, not synced)

Physical paths starting with ``/workspace/myspace/<current_user_id>/`` are
treated as the "myspace persistent area": after Write/Edit completes they are
immediately reverse-synced to the artifact table + myspace_cache.
"""

from __future__ import annotations

from typing import Optional

# Workspace root: ``/workspace`` inside the Docker sandbox; the no-Docker local
# profile points the host script_runner at a real dir via SCRIPT_RUNNER_WORKSPACE.
# Read the single source so the physical paths these file tools build match the
# sidecar's own validation root (else host-mode Read/Write/Edit hit HTTP 400).
from core.sandbox._common import WORKSPACE as WORKSPACE_ROOT

SCRATCH_ROOT = f"{WORKSPACE_ROOT}/scratch"
MYSPACE_LOGICAL = "/myspace"

# Model-facing paths are written against the container-canonical ``/workspace``
# root — the system prompt, skill text, and plugin scripts all say ``/workspace``.
# In the no-Docker local profile the real root differs (e.g. ~/.hugagent/workspace),
# so alias a leading ``/workspace`` → WORKSPACE_ROOT at the one chokepoint every
# file tool passes through. No-op (byte-identical) when WORKSPACE_ROOT == /workspace.
_CANON_WS = "/workspace"


def canonicalize_ws_path(path: str) -> str:
    """Rewrite a canonical ``/workspace[/...]`` path to the real workspace root."""
    if not isinstance(path, str) or WORKSPACE_ROOT == _CANON_WS:
        return path
    if path == _CANON_WS:
        return WORKSPACE_ROOT
    if path.startswith(_CANON_WS + "/"):
        return WORKSPACE_ROOT + path[len(_CANON_WS):]
    return path


# The **single** path-policy reminder shared by the seven file tools. The full
# rules live in exactly one place, the system prompt (code_exec's
# 10_tools_and_capabilities); only a one-line pointer is kept here, to avoid
# copying the whole policy into 7 tool descriptions and repeating it 8 times
# per request. Core: sandbox first; touch /myspace only when the user
# explicitly asks.
PATH_POLICY_DOC = (
    "【路径】默认在沙盒 ``/workspace/`` 工作（临时、用户看不到、可随意增删改）；"
    "**仅当用户明确要操作他的「我的空间」/已存文件/要保存留档时**才读写 "
    "``/myspace/``，否则不主动写改删它。完整规则见系统提示词「路径策略」。"
)


def _is_traversal_or_bad(path: str) -> Optional[str]:
    """Reject traversal/double-slash early. Returns error message or None."""
    if not path or not isinstance(path, str):
        return "path 必须为非空字符串"
    if "/../" in path or path.endswith("/..") or "//" in path:
        return f"path 不允许包含 .. 或 //: {path}"
    return None


def validate_workspace_path(path: str) -> Optional[str]:
    """Accept ``/myspace/...``, ``/workspace/...``; reject anything else.

    Returns ``None`` on success, an error string on rejection.
    """
    err = _is_traversal_or_bad(path)
    if err:
        return err
    path = canonicalize_ws_path(path)
    if path == MYSPACE_LOGICAL or path.startswith(MYSPACE_LOGICAL + "/"):
        return None
    if path == WORKSPACE_ROOT or path.startswith(WORKSPACE_ROOT + "/"):
        return None
    return (
        f"path 必须以 /myspace/ 或 /workspace/ 开头（持久文件用 /myspace/，"
        f"临时计算用 /workspace/scratch/）；got: {path}"
    )


def validate_project_scope_path(path: str, project_folder_name: Optional[str]) -> Optional[str]:
    """Extra constraint in project mode: ``/myspace/`` paths must fall under the hooked folder.

    - ``project_folder_name`` is ``None`` / empty → not project mode, no check;
    - ``/workspace/`` paths are the sandbox temp area, exempt from the project sandbox constraint;
    - ``/myspace/<x>/...`` requires ``<x>`` to equal ``project_folder_name``.
    """
    if not project_folder_name:
        return None
    if not path or not isinstance(path, str):
        return None  # validate_workspace_path already rejected
    if not (path == MYSPACE_LOGICAL or path.startswith(MYSPACE_LOGICAL + "/")):
        return None
    rest = path[len(MYSPACE_LOGICAL):].lstrip("/")
    first = rest.split("/", 1)[0] if rest else ""
    if first == project_folder_name:
        return None
    return (
        f"项目模式下文件工具只能操作 /myspace/{project_folder_name}/ 下的文件，"
        f"不能访问 /myspace/{first or '(根)'}/。"
    )


def to_physical_path(path: str, user_id: Optional[str]) -> str:
    """Translate a logical ``/myspace/...`` path to its physical sandbox path.

    - ``/myspace`` → ``/workspace/myspace/{user_id}``
    - ``/myspace/<rest>`` → ``/workspace/myspace/{user_id}/<rest>``
    - Other paths (already physical) → unchanged.

    If ``user_id`` is missing for a logical path, returns the input unchanged
    (caller should validate user_id presence before calling).
    """
    path = canonicalize_ws_path(path)
    if path == MYSPACE_LOGICAL:
        if not user_id:
            return path
        return f"{WORKSPACE_ROOT}/myspace/{user_id}"
    if path.startswith(MYSPACE_LOGICAL + "/"):
        if not user_id:
            return path
        rest = path[len(MYSPACE_LOGICAL) + 1:]
        return f"{WORKSPACE_ROOT}/myspace/{user_id}/{rest}"
    return path


def is_myspace_physical(physical_path: str, user_id: Optional[str]) -> bool:
    """Is this physical path inside the current user's myspace persistent area?

    Both ``/workspace/myspace/{user_id}/foo`` and the logical ``/myspace/foo``
    (after translation) end up here.
    """
    if not user_id:
        return False
    prefix = f"{WORKSPACE_ROOT}/myspace/{user_id}/"
    return physical_path == prefix.rstrip("/") or physical_path.startswith(prefix)


# Legacy alias kept for callers (Write tool used the old name).
def is_in_myspace(path: str, user_id: Optional[str]) -> bool:
    return is_myspace_physical(path, user_id)


def parent_dir(path: str) -> str:
    """Return the parent directory portion of ``path`` (no trailing slash)."""
    if "/" not in path:
        return WORKSPACE_ROOT
    return path.rsplit("/", 1)[0] or WORKSPACE_ROOT


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]

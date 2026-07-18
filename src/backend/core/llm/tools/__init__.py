"""File-operation tools for the agent's code capability.

These tools mirror Claude Code's FileReadTool / FileEditTool / FileWriteTool /
GlobTool / GrepTool but operate on the sandbox filesystem (via
``SandboxProvider.get_file`` / ``put_file`` / ``execute``) instead of the host.

All 5 tools are registered together inside the ``code_capability_enabled()``
block in ``agent_factory.py``. Read/Edit/Write share a per-chat
``ReadStateTracker`` to enforce the "must Read before Edit/Write" invariant.
"""

from ._state import ReadEntry, ReadStateTracker
from .edit_tool import register_edit
from .fileops_tool import register_delete, register_mkdir, register_move
from .glob_tool import register_glob
from .grep_tool import register_grep
from .read_tool import register_read
from .write_tool import register_write

# Sandbox / skill / myspace / artifact tools (relocated from the former
# singular ``core.llm.tool`` module). Surfaced here so callers import the whole
# tool family from one place: ``from core.llm.tools import register_bash, ...``.
from .sandbox_tool import (
    register_bash,
    register_sandbox_get_artifact,
    register_sandbox_put_artifact,
)
from .skill_tool import (
    register_sandboxed_view_text_file,
)
from .myspace_tool import register_myspace_tools
from .pin_tool import register_pin_to_workspace
from .read_artifact_tool import register_read_artifact
from .data_context_tool import register_get_data_context

__all__ = [
    "ReadEntry",
    "ReadStateTracker",
    "register_bash",
    "register_get_data_context",
    "register_delete",
    "register_edit",
    "register_glob",
    "register_grep",
    "register_mkdir",
    "register_move",
    "register_myspace_tools",
    "register_pin_to_workspace",
    "register_read",
    "register_read_artifact",
    "register_sandbox_get_artifact",
    "register_sandbox_put_artifact",
    "register_sandboxed_view_text_file",
    "register_write",
]

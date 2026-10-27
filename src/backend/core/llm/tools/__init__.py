"""File-operation tools for the agent's code capability.

These tools mirror Claude Code's FileReadTool / FileEditTool / FileWriteTool /
GlobTool / GrepTool but operate on the sandbox filesystem (via
``SandboxProvider.get_file`` / ``put_file`` / ``execute``) instead of the host.

All 5 tools are registered together inside the ``code_capability_enabled()``
block in ``agent_factory.py``. Like pi-mono's coding agent, Edit/Write do not
require a prior Read; Edit/Write on the same file are serialized.
"""

from .edit_tool import register_edit
from .glob_tool import register_glob
from .grep_tool import register_grep
from .read_image_tool import register_read_image
from .read_tool import register_read
from .write_tool import register_write

# Sandbox / skill / myspace / artifact tools, surfaced here so callers import
# the whole tool family from one place: ``from core.llm.tools import register_bash, ...``.
from .sandbox_tool import (
    register_bash,
    register_sandbox_get_artifact,
    register_sandbox_put_artifact,
)
from .skill_tool import (
    register_sandboxed_view_text_file,
)
from .chat_history_tool import register_chat_history_tools
from .pin_tool import register_pin_to_workspace
from .read_artifact_tool import register_read_artifact

__all__ = [
    "register_bash",
    "register_chat_history_tools",
    "register_edit",
    "register_glob",
    "register_grep",
    "register_pin_to_workspace",
    "register_read",
    "register_read_artifact",
    "register_read_image",
    "register_sandbox_get_artifact",
    "register_sandbox_put_artifact",
    "register_sandboxed_view_text_file",
    "register_write",
]

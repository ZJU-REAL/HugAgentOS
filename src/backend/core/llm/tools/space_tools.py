"""Register the complete space file-tool family through one entry point."""

from typing import Optional

from agentscope.tool import Toolkit

from core.services.project_scope import ProjectScope

from ._state import ReadStateTracker
from .fileops_tool import register_delete, register_mkdir, register_move
from .myspace_tool import register_myspace_tools


def register_space_tools(
    toolkit: Toolkit,
    *,
    user_id: Optional[str],
    state: ReadStateTracker,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    interactive: bool = True,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:
    """Register discovery, staging and mutations with the same project scope.

    Personal tools and the edition-specific team listing share this entry point.
    Every mutation keeps its declarative permission and approval policy.
    """
    if not user_id:
        return
    common = dict(
        chat_id=chat_id,
        sandbox_session_id=sandbox_session_id,
        user_id=user_id,
        interactive=interactive,
        project_folder_name=project_folder_name,
        scope=scope,
    )
    register_delete(toolkit, state=state, **common)
    register_move(toolkit, state=state, **common)
    register_mkdir(toolkit, **common)
    register_myspace_tools(toolkit, user_id=user_id, scope=scope)

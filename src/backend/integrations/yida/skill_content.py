"""Resolve the Yida workspace example for the authenticated local account."""
import shlex
from core.config.local_mode import local_mode_enabled
from core.sandbox.workspace.identity import safe_user_id
from integrations.yida.execution import workspace_dir


def adapt(content: str, user_id: str | None) -> str:
    if (local_mode_enabled() and user_id and safe_user_id(user_id)
            and "/home/ubuntu/yida-workspace" in content):
        return content.replace("/home/ubuntu/yida-workspace", shlex.quote(workspace_dir(user_id).as_posix()))
    return content

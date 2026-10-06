"""Installed workers inherit the local permission preset at process creation."""
import os
import sys
from pathlib import Path

def launch_policy(provider, user_id, chat_id):
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled() or not provider.runs_on_host:
        return None
    from core.llm.tool_permissions import resolve_approval_mode, LocalCommandAuthorization
    from core.services.local_grant_service import policy_for_gate
    from core.sandbox.os_sandbox import LocalAccessDecision, protected_read_paths
    from core.sandbox.local_policy import NETWORK
    from core.llm.tools._paths import workspace_directory
    mode = resolve_approval_mode(None, user_id=user_id)
    policy = policy_for_gate(mode)
    if policy.disposition_for(NETWORK) == "block":
        raise ValueError("interactive_runtime_network_blocked")
    workspace = workspace_directory(chat_id)
    # Mount plans must see the session directory before the runner stages files.
    Path(workspace).mkdir(parents=True, exist_ok=True)
    # These are trusted runtime dependencies, not a grant to the capability DB.
    readable = [sys.prefix]
    browsers = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
    if browsers:
        readable.append(str(Path(browsers).resolve()))
    access = LocalAccessDecision(
        approval_mode=mode, unconfined=mode == "full",
        writable_roots=(workspace,), readable_roots=tuple(readable),
        denied_paths=protected_read_paths(),
        network_allowed=True, writable_scratch=True,
    )
    return LocalCommandAuthorization("installed interactive runtime", mode, access, workspace).confine()

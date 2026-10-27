"""Installed workers inherit the local permission preset at process creation."""
import os
import sys
from pathlib import Path

def launch_policy(provider, user_id, chat_id):
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled() or not provider.runs_on_host:
        return None
    from core.sandbox.permissions.account import resolve_work_approval_mode
    from core.sandbox.permissions.dispatch_models import LocalCommandAuthorization
    from core.harness.registry.local_files import policy_for_gate
    from core.sandbox.permissions.os_gate import LocalAccessDecision
    from core.sandbox.permissions.os_gate import protected_read_paths
    from core.sandbox.permissions.models import NETWORK
    from core.sandbox.workspace.resolution import workspace_directory
    # A client-session lease, not the account, governs work in a leased chat.
    mode = resolve_work_approval_mode(user_id, chat_id)
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

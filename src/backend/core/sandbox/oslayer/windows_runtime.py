"""Applying a Windows sandbox plan at the moment a process is spawned.

The Windows confinement cannot be expressed as an argv prefix — a token has to
be built and handed to ``CreateProcessAsUser`` by whoever creates the process.
So instead of a wrapper command, the backend hands the spawning boundary a
*plan*, and this module turns that plan into a running process.

Two design points are worth keeping:

* **The plan is already decided.** The backend resolved the policy before
  serializing it; nothing here re-derives access from a policy. Resolving on
  both sides of the boundary would mean two copies of the same decision, and the
  second copy is the one no test of the permission layer can see.
* **Nothing here needs the policy model.** The runner imports this module once
  at startup, but keeping its dependencies to the standard library and the
  ``ctypes`` bindings is what lets the same module be reached from a short-lived
  process too, if one is ever needed again.

The plan is small:

``writable_roots``
    Each an absolute path the command may write, with the paths inside it that
    must stay read-only — the policy's own read-only entries and the protected
    metadata names, already joined into one list.
``scratch_dir``
    The private directory that stands in for the user's temp folder, or absent
    when the policy grants no scratch space. See
    :func:`core.sandbox.oslayer.windows_token.scratch_directory` for why the
    real temp folder is not used.
``state_dir``
    Where the capability SIDs are remembered between runs.
"""

from __future__ import annotations

import os

# Environment variables Windows programs read to find their temp directory.
TEMP_ENV_KEYS = ("TEMP", "TMP")


def apply_filesystem_grants(plan: dict) -> list:
    """Write the ACEs that pair with the token; return the capability SIDs.

    A grant on a writable root is inheritable, so the read-only paths inside it
    get an explicit deny for the same SID afterwards. Deny entries are ordered
    ahead of grants in the resulting ACL, so the narrower rule wins.

    Read-only paths that do not exist yet are skipped: an ACE needs an object to
    sit on, and there is no existing authority to protect at a path nothing has
    created.
    """
    from .win32.acl import deny_write_access, grant_write_access
    from .win32.caps import capability_sids_for_roots, readonly_capability_sid

    state_dir = plan["state_dir"]
    roots = plan.get("writable_roots") or []
    if not roots:
        # A read-only plan still needs a restricting SID for the token to carry
        # — one that is granted nowhere, so nothing becomes writable.
        return [readonly_capability_sid(state_dir)]

    paths = [root["path"] for root in roots]
    sids = capability_sids_for_roots(state_dir, paths)
    for root in roots:
        sid = sids[root["path"]]
        grant_write_access(root["path"], sid)
        for read_only in root.get("read_only") or ():
            if os.path.exists(read_only):
                deny_write_access(read_only, sid)
    return [sids[path] for path in paths]


def child_environment(plan: dict, env: dict) -> dict:
    """The environment the command runs with, scratch space redirected.

    Returned as a new mapping rather than applied to this process: the caller is
    a long-lived server, and rewriting its own ``TEMP`` would leak one command's
    scratch directory into every other command it is running.
    """
    scratch = plan.get("scratch_dir")
    if not scratch:
        return dict(env)
    return {**env, **{key: scratch for key in TEMP_ENV_KEYS}}


def spawn_confined(
    plan: dict,
    command: list,
    *,
    cwd: str | None,
    env: dict,
    stdin: int,
    stdout: int,
    stderr: int,
):
    """Apply ``plan`` and start ``command`` under the restricted token it implies.

    Returns a :class:`core.sandbox.oslayer.win32.spawn.TokenProcess`, which the
    caller must ``close()`` once it has read the exit code.
    """
    from .win32 import ffi
    from .win32.spawn import spawn_with_token
    from .win32.token import create_restricted_token, open_current_process_token

    scratch = plan.get("scratch_dir")
    if scratch:
        os.makedirs(scratch, exist_ok=True)

    capability_sids = apply_filesystem_grants(plan)
    base_token = open_current_process_token()
    try:
        token = create_restricted_token(base_token, capability_sids)
    finally:
        ffi.CloseHandle(base_token)
    try:
        return spawn_with_token(
            token,
            list(command),
            cwd=cwd,
            env=child_environment(plan, env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        ffi.CloseHandle(token)


__all__ = [
    "TEMP_ENV_KEYS",
    "apply_filesystem_grants",
    "child_environment",
    "spawn_confined",
]

"""State owned by one persistent OpenSandbox session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class _Session:
    sandbox: Any  # opensandbox.Sandbox
    interpreter: Any  # code_interpreter.CodeInterpreter
    contexts: dict[str, Any]  # language → CodeContext (one ctx per language, mutually isolated)
    # The user_id owning this sandbox (binds MySpace/account-credential volumes +
    # user re-homing on adopt across restarts). None when legacy paths call
    # _get_or_create_session without a user_id — execution then falls back to a
    # credential-less general sandbox.
    user_id: Optional[str] = None
    # Plan F: which pool this sandbox was actually taken from. **release must follow this,
    # never re-decide from the current user-bound-volume settings** — a mid-flight
    # flag change (ops toggle, hot reload) would route release to a different pool,
    # corrupting counts / cross-user contamination. The snapshot restore path builds
    # directly via _create_jupyter_sandbox without any pool acquire ("none"); on destroy it
    # destroys outright instead of calling either pool's release — calling _pool.release
    # would push _pool.in_flight negative (no paired acquire), and calling
    # _jupyter_user_pool.release would leak the previous chat's /workspace/scratch to the
    # same user's next chat. Three states: 'user' | 'general' | 'none'
    pool_source: str = "none"
    setup_done: bool = (
        False  # whether the directory conventions (scratch/myspace/symlinks) are set up
    )
    # 已建好 /myspace 软链的 uid。不带 user_id 的调用方会让 setup 跳过 myspace 那一半，
    # 只看 setup_done 会把残缺状态当成完成，并随沙箱回到空闲池污染后续会话。
    myspace_link_uid: Optional[str] = None
    last_renew_ts: float = 0.0
    # Monotonic clock of the last business request (execute/put_file/get_file). Used for
    # idle reaping: a background task proactively destroys persistent sessions whose
    # now - last_active_ts exceeds the threshold, instead of waiting for the server TTL.
    last_active_ts: float = 0.0
    # Set True when the background async renew reports a lifecycle signal
    # (404/422/terminated); the next _get_or_create_session destroys and rebuilds the
    # moment it takes the lock, avoiding a 3s synchronous renew on the request path.
    stale_marked: bool = False
    # Consecutive renew failure count: used to escalate "persistent ReadTimeout /
    # ConnectError" to stale as well. A single timeout may be transient server-side high
    # load and should not destroy a healthy session; but N consecutive failures usually
    # mean the sandbox is actually dead while the server still lists it as existing (the
    # window before the 404). A successful _renew resets it to 0. See
    # _renew_in_background for the N threshold.
    consecutive_renew_failures: int = 0

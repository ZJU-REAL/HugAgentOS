"""State owned by one persistent OpenSandbox session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SandboxSessionRecord:
    sandbox: Any  # opensandbox.Sandbox
    # None identifies an anonymous session without account volumes.
    user_id: Optional[str] = None
    # Which pool this sandbox was actually taken from. **release must follow this,
    # never re-decide from the current user-bound-volume settings** — a mid-flight
    # flag change (ops toggle, hot reload) would route release to a different pool,
    # corrupting counts / cross-user contamination. The snapshot restore path builds
    # directly via _create_jupyter_sandbox without any pool acquire ("none"); on destroy it
    # destroys outright instead of calling either pool's release — calling _pool.release
    # would push _pool.in_flight negative (no paired acquire), and calling
    # _jupyter_user_pool.release would leak the previous chat's /workspace/scratch to the
    # same user's next chat. Sources: 'user' | 'general' | 'none' | 'attached'
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
    # (structured DOCKER::SANDBOX_NOT_FOUND); the next _get_or_create_session destroys and rebuilds the
    # moment it takes the lock, avoiding a 3s synchronous renew on the request path.
    stale_marked: bool = False
    # Diagnostic only: transient failures never authorize destruction.
    consecutive_renew_failures: int = 0

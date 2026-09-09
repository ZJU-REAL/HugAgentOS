"""Best-effort desktop read preparation after an account manifest finishes syncing.

No agent, model call or task is created here. Execution still owns the live
permission and fresh integrity gate; these caches only avoid first-use work.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

logger = logging.getLogger(__name__)


def warmup_current_account(state):
    from . import skills, registry, store
    from .dependency import component_hash, skill_definition
    from .paths import capabilities_enabled, LOCAL_PROFILE, BUILTIN_PROFILE
    from core.services.desktop_cloud_bridge import get_state, _state_fingerprint

    if os.name != "nt" or not capabilities_enabled():
        return
    started = time.perf_counter_ns()
    identity = _state_fingerprint(state)

    def current():
        return identity == _state_fingerprint(get_state())

    if not current():
        return
    user_id = skills.current_local_user_id()
    profile = skills.current_account_profile()
    if not user_id or not profile or not skills.account_authorized_for(user_id):
        return
    rows = [
        row
        for row in registry.list_installations()
        if row.kind in ("skill", "plugin", "agent")
        and row.ready
        and row.enabled
        and row.profile_id in (LOCAL_PROFILE, BUILTIN_PROFILE, profile)
        and (not row.payload.get("owner_user_id") or str(row.payload["owner_user_id"]) == user_id)
    ]

    def read(row):
        if not current():
            return
        component = store.get(row.kind, row.profile_id, row.key, row.resolved_revision)
        if component is not None:
            component_hash(component, fresh=True)
            if row.kind == "skill":
                skill_definition(component.path)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-startup-read") as pool:
        futures = [pool.submit(copy_context().run, read, row) for row in rows]
        for future in futures:
            future.result()
    if current():
        skills.resolve_for_user(user_id)
        logger.info(
            "[caps] startup reads ready: %d packages, %.3fms",
            len(rows),
            (time.perf_counter_ns() - started) / 1e6,
        )

"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from core.config.settings import settings

from . import session_registry
from ._common import SANDBOX_RUN_GID, SANDBOX_RUN_UID, safe_user_id
from .errors import SandboxConnectError, SandboxError, SandboxTimeoutError

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
# Provider internals moved to core.sandbox._opensandbox_internals; import back
from core.sandbox._opensandbox_internals import (  # noqa: F401,E402
    _get_db_session_local,
    _get_snapshot_model,
    logger,
    _SNAPSHOT_STATE_READY,
    _SNAPSHOT_TERMINAL_FAILURE_STATES,
    _LIGHT_ENTRYPOINT,
    _SETUP_ENTRYPOINT,
    _pool_metadata,
    _make_skills_volumes,
    _make_dws_creds_volumes,
    _make_lark_creds_volumes,
    _make_email_creds_volumes,
    _make_yida_creds_volumes,
    _user_bound_sandbox_required,
    _Session,
)


from ._opensandbox_creation import _OpenSandboxCreationMixin
from ._opensandbox_snapshots import _OpenSandboxSnapshotsMixin
from ._opensandbox_lifecycle import _OpenSandboxLifecycleMixin

class _OpenSandboxSessionMixin(_OpenSandboxCreationMixin, _OpenSandboxSnapshotsMixin, _OpenSandboxLifecycleMixin):
    pass

"""Serialize Docker admission, not sandbox initialization or mount recovery."""
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException


@contextmanager
def sandbox_capacity(service):
    # File locking also fences multiple controller processes sharing the volume.
    # Docker inventory survives controller/backend restart; no reservation ledger.
    root = Path("/var/lib/opensandbox")
    root.mkdir(parents=True, exist_ok=True)
    namespace = hashlib.sha256(service.network_mode.encode()).hexdigest()[:24]
    limit = int(os.environ.get("OPENSANDBOX_POOL_MAX_TOTAL", "20"))
    if limit < 1:
        raise HTTPException(503, "Sandbox capacity must be positive")
    with (root / ("capacity-" + namespace + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(429, "Sandbox creation busy; retry later") from None
        try:
            if len(service._owned_sandboxes()) >= limit:
                raise HTTPException(429, "Deployment sandbox capacity reached")
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

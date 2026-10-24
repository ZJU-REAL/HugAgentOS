"""client state responsibilities."""
from __future__ import annotations
import threading
from typing import Any, Dict, Optional, Tuple

BRIDGE_BLOCK_ID = "desktop_cloud_bridge"


MANIFEST_NEG_TTL_S = 30.0


state_lock = threading.RLock()


state: Optional[Dict[str, Any]] = None  # {"cloud_base", "token", "expires_at"}


credential_rejected = ""  # account fingerprint whose current token the cloud refused


state_loaded = False


manifest_lock = threading.Lock()


tool_sync_lock = threading.Lock()


manifest: Optional[Dict[str, Any]] = None


manifest_ts: float = 0.0


manifest_error: Optional[str] = None


manifest_fetching = False


refresh_pending = False  # a forced refresh arrived while a pass was running


context_lock = threading.Lock()


context_cache: Optional[Tuple[tuple, Dict[str, Any]]] = None

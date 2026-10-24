"""Per-user OpenSandbox credential mounts."""
from __future__ import annotations
import logging
import re
from typing import Any, Optional
from core.config.settings import settings
from core.sandbox.workspace.identity import safe_user_id
from integrations.yida.storage import yida_cache_dir, YIDA_WORKSPACE_MOUNT
logger = logging.getLogger(__name__)

_YIDA_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under yida_cache/{uid}, sandbox mount point)
    ("workspace", YIDA_WORKSPACE_MOUNT),
)


def mounts(user_id: Optional[str]) -> list[Any]:
    """Build the read-write bind-mount Volume for openyida login-state persistence (one per user, single directory).

    Same approach as [[_make_email_creds_volumes]]: flag off / invalid user_id /
    host_storage not configured / SDK has no Volume model — any unmet condition quietly
    degrades to "no credential volume mounted"; openyida then has no login state in the
    sandbox, business commands fail as not-logged-in, and the yida skill guides the user
    through QR-code login in the conversation (still usable within the session, just not
    surviving across sessions). Pre-create the host directories as backend (UID 1000) to
    avoid docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not settings.sandbox.yida_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        logger.warning(
            "[opensandbox] yida creds bind mount enabled but HOST_STORAGE_PATH "
            "not configured; openyida will have no persisted login state"
        )
        return []
    cache_dir = yida_cache_dir(uid)
    try:
        for sub, _mount in _YIDA_CREDS_SUBPATHS:
            (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[opensandbox] failed to pre-create yida_cache dir %s: %s; "
            "skipping yida creds bind mount",
            cache_dir,
            exc,
        )
        return []
    try:
        from opensandbox.models.sandboxes import Host, Volume
    except ImportError:
        return []
    slug = re.sub(r"[^a-z0-9-]", "-", uid.lower()).strip("-") or "u"
    out: list[Any] = []
    for idx, (sub, mount_path) in enumerate(_YIDA_CREDS_SUBPATHS):
        vol_name = f"yida{idx}-{slug}"[:63].rstrip("-") or f"yida{idx}"
        out.append(
            Volume(
                name=vol_name,
                host=Host(path=f"{host_storage}/yida_cache/{uid}/{sub}"),
                mount_path=mount_path,
                read_only=False,
            )
        )
    return out

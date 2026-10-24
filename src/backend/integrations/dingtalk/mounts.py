"""Per-user OpenSandbox credential mounts."""
from __future__ import annotations
import logging
import re
from typing import Any, Optional
from core.config.settings import settings
from core.sandbox.workspace.identity import safe_user_id
from integrations.dingtalk.storage import dws_cache_dir
logger = logging.getLogger(__name__)

_DWS_HOME = "/home/ubuntu"


_DWS_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under dws_cache/{uid}, sandbox mount point)
    # Matches the backend login layout HOME=dws_cache/{uid}/home: login writes home/.dws and
    # home/.local/share/dws-cli; here we bind those two directories into the sandbox ubuntu
    # user's home.
    ("home/.dws", f"{_DWS_HOME}/.dws"),
    ("home/.local/share/dws-cli", f"{_DWS_HOME}/.local/share/dws-cli"),
)


def mounts(user_id: Optional[str]) -> list[Any]:
    """Build the two read-write bind-mount Volumes for dws credential persistence (one set per user).

    Empty list is returned when: flag off / invalid user_id / host_storage not configured /
    SDK has no Volume model — any unmet condition quietly degrades to "no credential
    volumes mounted"; dws then has no credentials in the sandbox, business commands fail
    with exit code 2 (not logged in), and the dingtalk skill guides the user to
    "Settings → Connect DingTalk account" first. Same approach as _make_myspace_volume:
    pre-create the host directories as backend (UID 1000) to avoid docker's root
    auto-mkdir at bind time leaving the backend unable to sync/clean credentials.
    """
    if not settings.sandbox.dws_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        logger.warning(
            "[opensandbox] dws creds bind mount enabled but HOST_STORAGE_PATH "
            "not configured; dws will have no persisted credentials"
        )
        return []
    cache_dir = dws_cache_dir(uid)
    try:
        for sub, _mount in _DWS_CREDS_SUBPATHS:
            (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[opensandbox] failed to pre-create dws_cache dir %s: %s; "
            "skipping dws creds bind mount",
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
    for idx, (sub, mount_path) in enumerate(_DWS_CREDS_SUBPATHS):
        # vol name must match k8s naming (lowercase alphanumerics + hyphens); sub contains
        # / and ., so use an index instead.
        vol_name = f"dws{idx}-{slug}"[:63].rstrip("-") or f"dws{idx}"
        out.append(
            Volume(
                name=vol_name,
                host=Host(path=f"{host_storage}/dws_cache/{uid}/{sub}"),
                mount_path=mount_path,
                read_only=False,
            )
        )
    return out

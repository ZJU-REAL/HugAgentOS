"""Per-user OpenSandbox credential mounts."""
from __future__ import annotations
import logging
import re
from typing import Any, Optional
from core.config.settings import settings
from core.sandbox.workspace.identity import safe_user_id
from integrations.lark.storage import lark_cache_dir
logger = logging.getLogger(__name__)

_LARK_HOME = "/home/ubuntu"


_LARK_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under lark_cache/{uid}, sandbox mount point); matches the backend login
    # layout HOME=lark_cache/{uid}/home: login writes home/.lark-cli and
    # home/.local/share/lark-cli; here we bind them into the sandbox ubuntu home.
    ("home/.lark-cli", f"{_LARK_HOME}/.lark-cli"),
    ("home/.local/share/lark-cli", f"{_LARK_HOME}/.local/share/lark-cli"),
)


def mounts(user_id: Optional[str]) -> list[Any]:
    """Build the two read-write bind-mount Volumes for lark-cli credential persistence (one set per user).

    Same approach as [[_make_dws_creds_volumes]]: flag off / invalid user_id / host_storage
    not configured / SDK has no Volume model — any unmet condition quietly degrades to "no
    credential volumes mounted"; lark-cli then has no credentials in the sandbox, business
    commands fail as not-logged-in, and the lark-shared skill guides the user to complete
    auth login first. Pre-create the host directories as backend (UID 1000) to avoid
    docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not settings.sandbox.lark_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        logger.warning(
            "[opensandbox] lark creds bind mount enabled but HOST_STORAGE_PATH "
            "not configured; lark-cli will have no persisted credentials"
        )
        return []
    cache_dir = lark_cache_dir(uid)
    try:
        for sub, _mount in _LARK_CREDS_SUBPATHS:
            (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[opensandbox] failed to pre-create lark_cache dir %s: %s; "
            "skipping lark creds bind mount",
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
    for idx, (sub, mount_path) in enumerate(_LARK_CREDS_SUBPATHS):
        # vol name must match k8s naming (lowercase alphanumerics + hyphens); sub contains
        # / and ., so use an index instead.
        vol_name = f"lark{idx}-{slug}"[:63].rstrip("-") or f"lark{idx}"
        out.append(
            Volume(
                name=vol_name,
                host=Host(path=f"{host_storage}/lark_cache/{uid}/{sub}"),
                mount_path=mount_path,
                read_only=False,
            )
        )
    return out

"""Per-user OpenSandbox credential mounts."""
from __future__ import annotations
import logging
import re
from typing import Any, Optional
from core.config.settings import settings
from core.sandbox.workspace.identity import safe_user_id
from integrations.email.storage import email_cache_dir
logger = logging.getLogger(__name__)

_EMAIL_HOME = "/home/ubuntu"


_EMAIL_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under email_cache/{uid}, sandbox mount point); matches the backend
    # layout HOME=email_cache/{uid}/home: config lands in home/.config/himalaya; here we
    # bind it to the same path under the sandbox ubuntu home.
    ("home/.config/himalaya", f"{_EMAIL_HOME}/.config/himalaya"),
)


def mounts(user_id: Optional[str]) -> list[Any]:
    """Build the read-write bind-mount Volume for himalaya credential persistence (one per user, single directory).

    Same approach as [[_make_lark_creds_volumes]]: flag off / invalid user_id / host_storage
    not configured / SDK has no Volume model — any unmet condition quietly degrades to "no
    credential volume mounted"; himalaya then has no config in the sandbox, business
    commands fail as unconfigured, and the email skill guides the user to bind a mailbox on
    the plugin detail page first. Pre-create the host directories as backend (UID 1000) to
    avoid docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not settings.sandbox.email_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        logger.warning(
            "[opensandbox] email creds bind mount enabled but HOST_STORAGE_PATH "
            "not configured; himalaya will have no persisted credentials"
        )
        return []
    cache_dir = email_cache_dir(uid)
    try:
        for sub, _mount in _EMAIL_CREDS_SUBPATHS:
            (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[opensandbox] failed to pre-create email_cache dir %s: %s; "
            "skipping email creds bind mount",
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
    for idx, (sub, mount_path) in enumerate(_EMAIL_CREDS_SUBPATHS):
        vol_name = f"email{idx}-{slug}"[:63].rstrip("-") or f"email{idx}"
        out.append(
            Volume(
                name=vol_name,
                host=Host(path=f"{host_storage}/email_cache/{uid}/{sub}"),
                mount_path=mount_path,
                read_only=False,
            )
        )
    return out

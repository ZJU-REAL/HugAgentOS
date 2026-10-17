"""OpenSandbox provider internals — module-level helpers, manifest parsing,
session/pool helper classes. Extracted from opensandbox_provider.py to slim it;
the OpenSandboxProvider class imports these back. No dependency on the provider
class itself (no cycle).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional


from ._common import dws_cache_dir, email_cache_dir, lark_cache_dir, safe_user_id, yida_cache_dir


logger = logging.getLogger(__name__)
from . import _opensandbox_internals as runtime

# dws (DingTalk CLI) credential persistence volumes: bind per-user host directories to the
# two paths under the sandbox ubuntu user's home that dws reads/writes by default. Measured
# behavior: the dws keychain/cache only honors $HOME, env redirection has no effect, so these
# two absolute paths must be bind-mounted (see the _common.dws_cache_dir comment).
_DWS_HOME = "/home/ubuntu"
_DWS_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under dws_cache/{uid}, sandbox mount point)
    # Matches the backend login layout HOME=dws_cache/{uid}/home: login writes home/.dws and
    # home/.local/share/dws-cli; here we bind those two directories into the sandbox ubuntu
    # user's home.
    ("home/.dws", f"{_DWS_HOME}/.dws"),
    ("home/.local/share/dws-cli", f"{_DWS_HOME}/.local/share/dws-cli"),
)


def _make_dws_creds_volumes(user_id: Optional[str]) -> list[Any]:
    """Build the two read-write bind-mount Volumes for dws credential persistence (one set per user).

    Empty list is returned when: flag off / invalid user_id / host_storage not configured /
    SDK has no Volume model — any unmet condition quietly degrades to "no credential
    volumes mounted"; dws then has no credentials in the sandbox, business commands fail
    with exit code 2 (not logged in), and the dingtalk skill guides the user to
    "Settings → Connect DingTalk account" first. Same approach as _make_myspace_volume:
    pre-create the host directories as backend (UID 1000) to avoid docker's root
    auto-mkdir at bind time leaving the backend unable to sync/clean credentials.
    """
    if not runtime.settings.sandbox.dws_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
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
            cache_dir, exc,
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


def _firecrawl_extra_envs() -> dict[str, str]:
    """Inject deployment-level credentials for the firecrawl CLI (firecrawl-cli is preinstalled in the sandbox and authenticates via env vars).

    **The config source is the Config admin "System Config → firecrawl" DB config**
    (``firecrawl.api_key`` / ``firecrawl.api_url``, the sole authority; no fallback to
    .env/compose). Both modes are supported, decided by which item is configured:
    - Cloud: configure ``firecrawl.api_key`` → inject ``FIRECRAWL_API_KEY``.
    - Self-hosted: configure ``firecrawl.api_url`` → inject ``FIRECRAWL_API_URL`` (the CLI
      skips cloud auth when it sees api_url); if a self-hosted instance also needs a key,
      both items can coexist.
    If neither is configured return an empty dict (equivalent to the original behavior;
    firecrawl commands fail unauthenticated and guide the user to configure it in the
    admin console). All users share one config; takes effect within <=30s of saving
    (SystemConfigService 30s cache), no restart needed.
    """
    out: dict[str, str] = {}
    try:
        from core.services.system_config import SystemConfigService

        svc = SystemConfigService.get_instance()
        api_key = (svc.get("firecrawl.api_key") or "").strip()
        api_url = (svc.get("firecrawl.api_url") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[firecrawl] read firecrawl config failed: %s", exc)
        return out
    if api_key:
        out["FIRECRAWL_API_KEY"] = api_key
    if api_url:
        out["FIRECRAWL_API_URL"] = api_url
    return out


# lark-cli (Feishu/Lark CLI) credential persistence volumes: same structure as dws. On Linux
# lark-cli uses a file-based encrypted store (master.key + *.enc) and only honors these two
# directories under $HOME, so both absolute paths must be bind-mounted (see the
# _common.lark_cache_dir comment). Note: we do **not** inject LARK_APP_* via env — when
# LARK_APP_ID/SECRET are present in the env, lark-cli defaults to the tenant/bot token flow,
# breaking --as user; app config is instead seeded by the backend's config init into the
# per-user HOME (same directory as the user token, entering the sandbox with the volume).
_LARK_HOME = "/home/ubuntu"
_LARK_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under lark_cache/{uid}, sandbox mount point); matches the backend login
    # layout HOME=lark_cache/{uid}/home: login writes home/.lark-cli and
    # home/.local/share/lark-cli; here we bind them into the sandbox ubuntu home.
    ("home/.lark-cli", f"{_LARK_HOME}/.lark-cli"),
    ("home/.local/share/lark-cli", f"{_LARK_HOME}/.local/share/lark-cli"),
)


def _make_lark_creds_volumes(user_id: Optional[str]) -> list[Any]:
    """Build the two read-write bind-mount Volumes for lark-cli credential persistence (one set per user).

    Same approach as [[_make_dws_creds_volumes]]: flag off / invalid user_id / host_storage
    not configured / SDK has no Volume model — any unmet condition quietly degrades to "no
    credential volumes mounted"; lark-cli then has no credentials in the sandbox, business
    commands fail as not-logged-in, and the lark-shared skill guides the user to complete
    auth login first. Pre-create the host directories as backend (UID 1000) to avoid
    docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not runtime.settings.sandbox.lark_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
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
            cache_dir, exc,
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


# himalaya (email CLI) credential persistence volume: same structure as lark/dws. himalaya's
# config is a plain-file config.toml (containing account blocks + raw app-password lines) and
# only honors the config path, so binding this one directory suffices (no keychain, no
# secondary store). The app password is a user-supplied IMAP/SMTP app password written into
# config.toml by the backend; no env is injected in the sandbox (unlike the dingtalk Custom
# App / firecrawl, which need deployment-level envs).
_EMAIL_HOME = "/home/ubuntu"
_EMAIL_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under email_cache/{uid}, sandbox mount point); matches the backend
    # layout HOME=email_cache/{uid}/home: config lands in home/.config/himalaya; here we
    # bind it to the same path under the sandbox ubuntu home.
    ("home/.config/himalaya", f"{_EMAIL_HOME}/.config/himalaya"),
)


def _make_email_creds_volumes(user_id: Optional[str]) -> list[Any]:
    """Build the read-write bind-mount Volume for himalaya credential persistence (one per user, single directory).

    Same approach as [[_make_lark_creds_volumes]]: flag off / invalid user_id / host_storage
    not configured / SDK has no Volume model — any unmet condition quietly degrades to "no
    credential volume mounted"; himalaya then has no config in the sandbox, business
    commands fail as unconfigured, and the email skill guides the user to bind a mailbox on
    the plugin detail page first. Pre-create the host directories as backend (UID 1000) to
    avoid docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not runtime.settings.sandbox.email_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
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
            cache_dir, exc,
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


# openyida (Yida CLI) login-state persistence volume: unlike the "$HOME subpath" pattern of
# dws/lark/email — openyida's cookie/env config lands under the "project root" .cache/
# (projectRoot falls back to cwd); it honors neither $HOME nor any global config directory.
# So each user gets a fixed working directory bind-mounted into the sandbox, and the yida
# skill forces all openyida commands to cd into that directory before running (see the
# _common.yida_cache_dir comment).
_YIDA_WORKSPACE_MOUNT = "/home/ubuntu/yida-workspace"
_YIDA_CREDS_SUBPATHS: tuple[tuple[str, str], ...] = (
    # (host subpath under yida_cache/{uid}, sandbox mount point)
    ("workspace", _YIDA_WORKSPACE_MOUNT),
)


def _make_yida_creds_volumes(user_id: Optional[str]) -> list[Any]:
    """Build the read-write bind-mount Volume for openyida login-state persistence (one per user, single directory).

    Same approach as [[_make_email_creds_volumes]]: flag off / invalid user_id /
    host_storage not configured / SDK has no Volume model — any unmet condition quietly
    degrades to "no credential volume mounted"; openyida then has no login state in the
    sandbox, business commands fail as not-logged-in, and the yida skill guides the user
    through QR-code login in the conversation (still usable within the session, just not
    surviving across sessions). Pre-create the host directories as backend (UID 1000) to
    avoid docker's root auto-mkdir at bind time leaving the backend unable to sync/clean.
    """
    if not runtime.settings.sandbox.yida_creds_bind_mount_enabled:
        return []
    uid = safe_user_id(user_id or "")
    if not uid:
        return []
    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
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
            cache_dir, exc,
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

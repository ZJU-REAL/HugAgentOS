"""Shared constants and utility functions for sandbox providers.

opensandbox_provider and future persistent/isolated providers share the same
artifact extension whitelist, size limits, and myspace cache path rules,
keeping behavior aligned across providers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from shlex import quote as shell_escape  # re-export so providers can `from ._common import shell_escape`

from core.config.settings import settings

__all__ = [
    "ALLOWED_EXTENSIONS", "INTERPRETER_CMD", "MAX_FILE_COUNT", "MAX_FILE_SIZE",
    "MAX_OUTPUT_BYTES", "MAX_STDERR_BYTES", "MAX_TOTAL_FILE_SIZE",
    "STDIN_FILE", "USER_ID_RE", "WORKSPACE",
    "myspace_cache_dir", "team_cache_dir", "dws_cache_dir", "dws_home_dir",
    "lark_cache_dir", "lark_home_dir", "lark_app_home_dir",
    "email_cache_dir", "email_home_dir", "email_himalaya_config",
    "yida_cache_dir", "yida_workspace_dir", "yida_shared_workspace_dir",
    "safe_user_id", "shell_escape",
]

# Kept aligned with services/script_runner_service/server.py
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".csv", ".xlsx", ".xls", ".json", ".txt", ".pdf",
    ".html", ".htm", ".docx", ".pptx", ".md",
}
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024
MAX_FILE_COUNT = 20
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 10240

# Backend-side view of the sandbox workspace root. Stays ``/workspace`` for the
# Docker sidecar / opensandbox / cube containers (in-container absolute path). The
# no-Docker local profile runs script_runner as a host subprocess pointed at a
# real dir (e.g. ``~/.hugagent/workspace``) via ``SCRIPT_RUNNER_WORKSPACE`` — the
# CLI exports it once so both the backend and the sidecar child agree. Kept as the
# single source for every model-facing ``/workspace`` mention and path we build.
WORKSPACE = os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")
STDIN_FILE = f"{WORKSPACE}/.hugagent_stdin.json"

USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def safe_user_id(user_id: str | None) -> str:
    """Return the value unchanged if valid; return an empty string if invalid/empty.
    Uniform replacement for the scattered ``uid if uid and USER_ID_RE.match(uid) else ""`` idiom."""
    return user_id if user_id and USER_ID_RE.match(user_id) else ""

INTERPRETER_CMD = {
    "python": "python3 -u",
    "javascript": "node",
    "bash": "bash",
}


def myspace_cache_dir(user_id: str) -> Path:
    """Backend-local myspace cache directory. The persistent sandbox (opensandbox) seeds
    files from here into ``/workspace/myspace/{user_id}/`` when a session is first created,
    then syncs incrementally by mtime.
    """
    return settings.storage.root / "myspace_cache" / user_id


def dws_cache_dir(user_id: str) -> Path:
    """Backend-local dws (DingTalk CLI) credential persistence directory (one per user).

    Contains two subdirectories, bind-mounted into the sandbox DingTalk CLI's default
    ``$HOME`` paths:
    - ``dws/``       → sandbox ``/home/ubuntu/.dws`` (config + market cache + app.json)
    - ``share/``     → sandbox ``/home/ubuntu/.local/share/dws-cli`` (encrypted keychain:
                       ``dek`` + ``auth-token.enc``)

    Empirically: dws's encrypted keychain and the ``.dws`` cache only respect ``$HOME``;
    ``DWS_CONFIG_DIR`` only relocates identity.json/logs, and ``XDG_DATA_HOME`` is
    ignored — so login must run with a per-user ``$HOME`` and these two home subpaths
    must be bind-mounted, rather than relying on env redirection. The sandbox runs as
    ubuntu (UID 1000) and the backend is also 1000, so ownership matches
    (see [[project_dingtalk_dws_integration]]).
    """
    return settings.storage.root / "dws_cache" / user_id


def dws_home_dir(user_id: str) -> Path:
    """Per-user ``$HOME`` for dws login (the unified root for backend subprocesses +
    opensandbox bind sources).

    The backend runs ``dws auth login`` with this as ``HOME`` → credentials land in
    ``{home}/.dws`` and ``{home}/.local/share/dws-cli``; opensandbox bind-mounts these
    two subpaths into the same home paths inside the sandbox, so **that user's** session
    sandboxes (bucketed per user) see their own credentials. The root of multi-tenant
    isolation: each user gets an independent home directory, never shared.
    """
    return dws_cache_dir(user_id) / "home"


def lark_cache_dir(user_id: str) -> Path:
    """Backend-local lark-cli (Feishu/Lark CLI) credential persistence directory (one per user).

    Contains two subdirectories, bind-mounted into the sandbox lark-cli's default
    ``$HOME`` paths:
    - ``home/.lark-cli/``           → sandbox ``/home/ubuntu/.lark-cli`` (config.json
                                      app config + cache + logs)
    - ``home/.local/share/lark-cli/`` → sandbox ``/home/ubuntu/.local/share/lark-cli``
                                      (file-based encrypted store: ``master.key`` +
                                      ``appsecret_*.enc`` + per-user ``cli_*_ou_*.enc``)

    Empirically: on Linux, lark-cli does **not** use the OS keychain but a file-based
    encrypted store — it only respects these two directories under ``$HOME``;
    ``master.key`` is a local DEK, so decryption works wherever the files travel.
    Isomorphic to dws's "Linux file-based DEK" scheme, so the per-user bind-mount
    persistence is copied verbatim (see [[dws_cache_dir]] and
    internal design docs).
    The sandbox runs as ubuntu (UID 1000) and the backend is also 1000, so ownership matches.
    """
    return settings.storage.root / "lark_cache" / user_id


def lark_home_dir(user_id: str) -> Path:
    """Per-user ``$HOME`` for lark-cli login (the unified root for the backend
    config init / auth login subprocesses + opensandbox bind sources).

    The backend runs ``lark-cli config init`` (writes app config) and ``auth login``
    (device flow, writes user_access_token) with this as ``HOME`` → credentials land in
    ``{home}/.lark-cli`` and ``{home}/.local/share/lark-cli``; opensandbox bind-mounts
    these two subpaths into the same home paths inside the sandbox, so **that user's**
    session sandboxes see their own credentials. The root of multi-tenant isolation:
    each user gets an independent home directory, never shared.
    """
    return lark_cache_dir(user_id) / "home"


def lark_app_home_dir() -> Path:
    """Org-wide shared Feishu **app** config HOME (the admin's one-click init
    ``config init --new`` lands here).

    Contains ``~/.lark-cli/config.json`` + ``~/.local/share/lark-cli/{master.key,
    appsecret_*.enc}`` (the app foundation — contains **no user tokens whatsoever**).
    These three items are copied into every user's ``lark_home_dir`` as the app
    foundation for their ``auth login`` — the whole org shares one app, and the admin
    configures it only once.
    """
    return settings.storage.root / "lark_cache" / "__app__" / "home"


def email_cache_dir(user_id: str) -> Path:
    """Backend-local email (himalaya CLI) credential persistence directory (one per user).

    Contains one subdirectory, bind-mounted into the sandbox himalaya's default
    config path:
    - ``home/.config/himalaya/`` → sandbox ``/home/ubuntu/.config/himalaya``
      (``config.toml``: account blocks + IMAP/SMTP servers + auth-code raw lines, file mode 0600)

    Isomorphic to lark/dws's "Linux file-based credentials": himalaya config is plain
    files that only respect the config path, so the per-user bind-mount persistence is
    copied verbatim. No OS keychain, no OAuth token refresh — auth codes stay valid
    long-term (governed by the mail provider).
    See internal design docs.
    """
    return settings.storage.root / "email_cache" / user_id


def email_home_dir(user_id: str) -> Path:
    """Per-user ``$HOME`` for himalaya login (the unified root for the backend
    verification subprocess + opensandbox bind sources).

    The backend runs ``himalaya folder list`` with this as ``HOME`` as a connectivity
    check; the config lands at ``{home}/.config/himalaya/config.toml``; opensandbox
    bind-mounts this subdirectory into the same home path inside the sandbox. The root
    of multi-tenant isolation: each user gets an independent home directory, never shared.
    """
    return email_cache_dir(user_id) / "home"


def email_himalaya_config(user_id: str) -> Path:
    """Backend absolute path of this user's himalaya ``config.toml`` (under email_home_dir)."""
    return email_home_dir(user_id) / ".config" / "himalaya" / "config.toml"


def yida_cache_dir(user_id: str) -> Path:
    """Backend-local openyida (Yida CLI) login-state persistence directory (one per user).

    Contains one subdirectory, bind-mounted into the sandbox's fixed Yida working directory:
    - ``workspace/`` → sandbox ``/home/ubuntu/yida-workspace`` (openyida's projectRoot:
      ``.cache/cookies-*.json`` login cookies + ``.cache/openyida-envs.json`` env config +
      project artifacts such as ``prd/`` and schema cache)

    Difference from dws/lark: openyida's login state does **not** respect $HOME; it lands
    under the "project root"'s ``.cache/`` (projectRoot = the cwd at command execution
    time, see the cwd fallback of findProjectRoot in openyida lib/core/utils.js). So the
    persistence scheme is not binding home subpaths but pinning each user a fixed
    in-sandbox working directory and binding it wholesale: the skill forces all openyida
    commands to run inside that directory, so cookies survive across sessions with the
    volume. Login is completed by QR scan inside the conversation
    (``openyida login --agent-qr``); the backend runs no openyida subprocess. The sandbox
    runs as ubuntu (UID 1000) and the backend is also 1000, so ownership matches.
    """
    return settings.storage.root / "yida_cache" / user_id


def yida_workspace_dir(user_id: str) -> Path:
    """Backend absolute path of this user's fixed Yida working directory (opensandbox
    bind source / cube inject-and-return source).

    The sandbox mount point is fixed at ``/home/ubuntu/yida-workspace``; the yida skill
    requires all openyida commands to ``cd`` into that directory, so the ``.cache/``
    login state and project artifacts all land on the persistent volume.
    """
    return yida_cache_dir(user_id) / "workspace"


def yida_shared_workspace_dir() -> Path:
    """Yida working directory for the script-runner shared sandbox (**one per deployment**,
    not per-user).

    script-runner is a shared container with no user concept (the provider ignores
    user_id; rootfs is read_only) and cannot do per-user credential isolation — compose
    mounts this directory at the container's ``/home/ubuntu/yida-workspace``, so the Yida
    login state is shared deployment-wide. Only suitable for single-user/dev environments;
    multi-user production should use opensandbox (per-user bind-mount) or cube (per-user
    inject + return). The backend pre-creates it with 0777 at startup (runner uid 1001 ≠
    backend uid 1000; cross-uid writes need wide-open permissions).
    """
    return settings.storage.root / "yida_cache" / "__shared__" / "workspace"


def team_cache_dir(team_id: str) -> Path:
    """Backend-local team file cache directory (shared among members of the same team).

    Bytes the agent reads via ``/myspace/<linked folder>/...`` in team projects try to
    hit this cache first, then fall back to object storage; different members and
    different chats of the same team share one cache, reducing S3/OSS egress traffic.
    """
    return settings.storage.root / "team_cache" / team_id

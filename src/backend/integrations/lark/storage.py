"""Per-user integration credential storage paths."""
from pathlib import Path
from core.config.settings import settings

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

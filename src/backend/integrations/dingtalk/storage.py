"""Per-user integration credential storage paths."""
from pathlib import Path
from core.config.settings import settings

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

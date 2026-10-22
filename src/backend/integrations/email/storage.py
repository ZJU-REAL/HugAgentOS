"""Per-user integration credential storage paths."""
from pathlib import Path
from core.config.settings import settings

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

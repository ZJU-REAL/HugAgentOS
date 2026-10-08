"""Resource credentials use a deployment key or an owner-private local key."""
import base64
import hashlib
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

def cipher():
    secret = next((os.getenv(name, "").strip() for name in (
        "PLUGIN_RESOURCE_SECRET_KEY", "EMAIL_SECRET_KEY", "ADMIN_TOKEN"
    ) if os.getenv(name, "").strip()), "")
    if not secret:
        from core.config.local_mode import local_mode_enabled
        if not local_mode_enabled():
            raise ValueError("plugin_resource_encryption_key_required")
        from core.config.runtime_env import local_data_dir
        root = Path(local_data_dir())
        root.mkdir(parents=True, exist_ok=True)
        path = root / ".plugin-resource-key"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink():
                raise ValueError("resource_key_symlink_not_allowed")
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(48))
        secret = path.read_text().strip()
        if not secret:
            raise ValueError("resource_encryption_key_unavailable")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

def encrypt_secret(value):
    return cipher().encrypt(value.encode()).decode()

def decrypt_secret(value):
    if not value:
        return None
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None

"""Application-layer symmetric encryption helper (encrypt sensitive config/credentials before persisting).

Currently used for the email plugin's authorization code (IMAP/SMTP app
password) — it must be encrypted before being written to
``email_connections.secret_enc``, never stored in plaintext. Based on
``cryptography.Fernet`` (AES128-CBC + HMAC, with a built-in version prefix and
integrity check); the key is derived from a deployment-level secret:

    EMAIL_SECRET_KEY  →  otherwise ADMIN_TOKEN  →  otherwise a fixed dev placeholder (local dev only, with a warning)

Derivation uses SHA256 → urlsafe-base64 into a 32-byte Fernet key, so a
deployment secret of any length works. Changing the deployment secret makes old
ciphertext undecryptable (equivalent to credential invalidation, the user must
re-bind) — this is expected behavior.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Local-dev fallback key: used only when neither EMAIL_SECRET_KEY nor ADMIN_TOKEN is configured; emits a warning.
# In production always configure EMAIL_SECRET_KEY (or at least ADMIN_TOKEN), otherwise moving/reinstalling machines makes old ciphertext undecryptable.
_DEV_FALLBACK = "hugagent-email-dev-secret-please-override"
_warned = False


def _deployment_secret() -> str:
    global _warned
    sec = (os.environ.get("EMAIL_SECRET_KEY") or "").strip()
    if sec:
        return sec
    sec = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if sec:
        return sec
    if not _warned:
        logger.warning(
            "[crypto] 未配置 EMAIL_SECRET_KEY / ADMIN_TOKEN，邮箱授权码加密使用 dev 兜底密钥；"
            "生产请务必配置 EMAIL_SECRET_KEY，否则重装/换机后旧凭据无法解密。"
        )
        _warned = True
    return _DEV_FALLBACK


# The derived key comes from the deployment-level secret and is constant at runtime → lazily construct it once and reuse, avoiding the SHA256+Fernet init on every encrypt/decrypt.
_fernet_cache = None


def _fernet():
    global _fernet_cache
    if _fernet_cache is None:
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(hashlib.sha256(_deployment_secret().encode("utf-8")).digest())
        _fernet_cache = Fernet(key)
    return _fernet_cache


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a sensitive string, returning a storable ciphertext token (str). An empty string is encrypted as usual (no special handling)."""
    return _fernet().encrypt((plaintext or "").encode("utf-8")).decode("ascii")


def decrypt_secret(token: Optional[str]) -> Optional[str]:
    """Decrypt a token produced by ``encrypt_secret``. Returns None if it can't be decrypted (key changed / data corrupted / None)."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001  (InvalidToken etc. are all downgraded to None)
        logger.warning("[crypto] decrypt_secret failed: %s", type(exc).__name__)
        return None

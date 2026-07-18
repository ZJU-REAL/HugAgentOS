"""Local account password hashing wrapper (Argon2id)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

# Default parameters are strong enough to resist GPU cracking, ~50ms per call (fast enough not to slow interaction)
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,   # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Return an Argon2id encoded string, ready to store directly in the DB."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify the password; any failure returns False, never raises."""
    if not password or not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    """Lets callers decide whether a re-hash is needed when parameters are upgraded."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False

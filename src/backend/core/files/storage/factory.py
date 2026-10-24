"""Storage backend factory and utility functions."""
from __future__ import annotations
from core.infra.time import utc_now

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    from werkzeug.utils import secure_filename
except ModuleNotFoundError:
    import re

    def secure_filename(filename: str) -> str:
        """Fallback secure_filename implementation when werkzeug is unavailable."""
        normalized = (filename or "").strip().replace("\\", "/").split("/")[-1]
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", normalized)
        sanitized = sanitized.strip("._")
        return sanitized or "file"

from core.files.storage.protocol import StorageBackend
from core.files.storage.local import LocalStorageBackend

logger = logging.getLogger(__name__)


def get_storage_backend() -> StorageBackend:
    """
    Get the configured storage backend.

    Returns the appropriate storage backend based on the STORAGE_TYPE environment variable:
    - 'oss': OSSStorageBackend (Aliyun OSS)
    - 's3': S3StorageBackend
    - 'local' or unset: LocalStorageBackend (default)

    Cloud backend imports are confined to their branches (seam C4): when a
    module is missing (the CE tree has no s3/oss), this factory still works
    under the local configuration.
    """
    storage_type = os.getenv("STORAGE_TYPE", "local").lower()

    if storage_type == "oss":
        from core.files.storage.oss import OSSStorageBackend
        return OSSStorageBackend()
    elif storage_type == "s3":
        from core.files.storage.s3 import S3StorageBackend
        return S3StorageBackend()
    elif storage_type == "obs":
        from core.files.storage.obs import OBSStorageBackend
        return OBSStorageBackend()
    elif storage_type == "local":
        return LocalStorageBackend()
    raise ValueError("STORAGE_TYPE must be local, s3, oss or obs")


def generate_storage_key(
    env: str,
    user_id: str,
    category: str,
    filename: str,
    chat_id: Optional[str] = None
) -> str:
    """
    Generate a standardized storage key.

    Format with chat_id:
        {env}/{category}/{user_id}/{chat_id}/{timestamp}_{filename}

    Format without chat_id:
        {env}/{category}/{user_id}/{timestamp}_{filename}
    """
    timestamp = utc_now().strftime("%Y%m%d%H%M%S")

    safe_filename = secure_filename(filename)
    if not safe_filename:
        safe_filename = "file"

    parts = [env, category, user_id]
    if chat_id:
        parts.append(chat_id)
    parts.append(f"{timestamp}_{safe_filename}")

    return "/".join(parts)


def get_storage_category_for_resource(resource_type: str) -> str:
    """
    Map resource type to storage category.

    Examples:
        >>> get_storage_category_for_resource('artifact')
        'artifacts'
        >>> get_storage_category_for_resource('kb_document')
        'kb_documents'
    """
    category_map = {
        'artifact': 'artifacts',
        'kb_document': 'kb_documents',
        'upload': 'uploads',
        'export': 'exports',
        'temp': 'temp',
    }

    return category_map.get(resource_type, 'uploads')


# Module-level storage instance (lazy-loaded)


def get_storage() -> StorageBackend:
    """
    Get the global storage backend instance (lazy-loaded singleton).
    """
    if process_state().storage_instance is None:
        process_state().storage_instance = get_storage_backend()
    return process_state().storage_instance


@dataclass
class FactoryState:
    storage_instance: Optional[StorageBackend] = None


_STATE = FactoryState()


def process_state() -> FactoryState:
    return _STATE

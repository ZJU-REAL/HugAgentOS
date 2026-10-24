"""Object storage backend abstraction layer.

Re-exports all public symbols from the split modules for backwards compatibility
with ``from core.files.storage import get_storage, generate_storage_key`` etc.
"""

from core.files.storage.protocol import StorageBackend
from core.files.storage.local import LocalStorageBackend
from core.files.storage.factory import get_storage_backend, get_storage, generate_storage_key, get_storage_category_for_resource

__all__ = [
    "StorageBackend",
    "LocalStorageBackend",
    "get_storage_backend",
    "get_storage",
    "generate_storage_key",
    "get_storage_category_for_resource",
]

# Cloud backends re-exported on demand (seam C4): the CE tree physically lacks s3/oss, so the import must not crash.
try:
    from core.files.storage.s3 import S3StorageBackend  # noqa: F401
    __all__.append("S3StorageBackend")
except ModuleNotFoundError:
    pass
try:
    from core.files.storage.oss import OSSStorageBackend  # noqa: F401
    __all__.append("OSSStorageBackend")
except ModuleNotFoundError:
    pass

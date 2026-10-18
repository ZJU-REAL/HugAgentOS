"""Abstract base class for storage backends."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def upload(self, file_path: str, storage_key: str) -> str:
        """Upload a file to storage. Returns storage URL."""
        ...

    @abstractmethod
    def upload_bytes(self, content: bytes, storage_key: str) -> str:
        """Upload bytes to storage. Returns storage URL."""
        ...

    @abstractmethod
    def download(self, storage_key: str, local_path: str) -> None:
        """Download a file from storage to local path."""
        ...

    @abstractmethod
    def download_bytes(self, storage_key: str) -> bytes:
        """Download file content as bytes."""
        ...

    @abstractmethod
    def generate_presigned_url(self, storage_key: str, expires_in: int = 900) -> str:
        """Generate a presigned URL for direct access."""
        ...

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """Delete a file from storage."""
        ...

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        """Return False only for confirmed absence; raise StorageError on service failure."""
        ...

    def list_directory(self, prefix: str, *, cursor=None, limit=200):
        """List one directory without loading descendant objects."""
        raise NotImplementedError

    def stat_object(self, key: str):
        """Return object metadata, or None for confirmed absence."""
        raise NotImplementedError

    def make_directory(self, prefix: str) -> None:
        """Create an empty directory."""
        raise NotImplementedError

    def copy_object(self, source: str, destination: str) -> None:
        """Copy an object within this backend."""
        raise NotImplementedError

    def remove_directory(self, prefix: str) -> None:
        """Remove an empty directory."""
        raise NotImplementedError

"""Local filesystem storage backend for development."""

import logging
import os
import shutil
from pathlib import Path

from core.infra.exceptions import StorageError
from core.storage.protocol import StorageBackend

logger = logging.getLogger(__name__)


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend for development."""

    def __init__(self):
        self.base_path = Path(os.getenv("STORAGE_PATH", "./storage"))
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageBackend initialized with base_path: {self.base_path}")

    def _get_full_path(self, storage_key: str) -> Path:
        safe_key = storage_key.lstrip("/")
        full_path = (self.base_path / safe_key).resolve()
        try:
            full_path.relative_to(self.base_path.resolve())
        except ValueError:
            import hashlib
            safe_name = hashlib.sha256(storage_key.encode()).hexdigest()
            full_path = self.base_path / "sanitized" / safe_name
            logger.warning(f"Path traversal attempt detected: {storage_key}, using sanitized path")
        return full_path

    def upload(self, file_path: str, storage_key: str) -> str:
        try:
            source = Path(file_path)
            if not source.exists():
                raise StorageError(operation="upload", error=f"Source file not found: {file_path}")
            destination = self._get_full_path(storage_key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            logger.info(f"File uploaded to local storage: {storage_key}")
            return str(destination)
        except Exception as e:
            logger.error(f"Failed to upload file to local storage: {e}")
            raise StorageError(operation="upload", error=str(e))

    def upload_bytes(self, content: bytes, storage_key: str) -> str:
        try:
            destination = self._get_full_path(storage_key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            logger.info(f"Bytes uploaded to local storage: {storage_key}")
            return str(destination)
        except Exception as e:
            logger.error(f"Failed to upload bytes to local storage: {e}")
            raise StorageError(operation="upload", error=str(e))

    def download(self, storage_key: str, local_path: str) -> None:
        try:
            source = self._get_full_path(storage_key)
            if not source.exists():
                raise StorageError(operation="download", error=f"File not found: {storage_key}")
            destination = Path(local_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            logger.info(f"File downloaded from local storage: {storage_key}")
        except Exception as e:
            logger.error(f"Failed to download file from local storage: {e}")
            raise StorageError(operation="download", error=str(e))

    def download_bytes(self, storage_key: str) -> bytes:
        try:
            source = self._get_full_path(storage_key)
            if not source.exists():
                raise StorageError(operation="download", error=f"File not found: {storage_key}")
            content = source.read_bytes()
            logger.info(f"Bytes downloaded from local storage: {storage_key}")
            return content
        except StorageError:
            raise
        except Exception as e:
            logger.error(f"Failed to download bytes from local storage: {e}")
            raise StorageError(operation="download", error=str(e))

    def generate_presigned_url(self, storage_key: str, expires_in: int = 900) -> str:
        try:
            full_path = self._get_full_path(storage_key)
            if not full_path.exists():
                raise StorageError(operation="generate_presigned_url", error=f"File not found: {storage_key}")
            return f"file://{full_path.absolute()}"
        except Exception as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise StorageError(operation="generate_presigned_url", error=str(e))

    def delete(self, storage_key: str) -> None:
        try:
            file_path = self._get_full_path(storage_key)
            if file_path.exists():
                file_path.unlink()
                logger.info(f"File deleted from local storage: {storage_key}")
            else:
                logger.warning(f"File not found for deletion: {storage_key}")
        except Exception as e:
            logger.error(f"Failed to delete file from local storage: {e}")
            raise StorageError(operation="delete", error=str(e))

    def exists(self, storage_key: str) -> bool:
        try:
            return self._get_full_path(storage_key).exists()
        except Exception as e:
            logger.error(f"Failed to check file existence: {e}")
            return False

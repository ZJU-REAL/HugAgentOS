"""Local filesystem storage backend for development."""

import io
import logging
import os
import shutil
from pathlib import Path

from core.infra.exceptions import StorageError
from core.files.storage.protocol import StorageBackend
from core.files.storage.local_directory import LocalDirectoryOperations
from core.files.storage.entries import validate_key
from core.files.storage import local_paths

logger = logging.getLogger(__name__)


class LocalStorageBackend(LocalDirectoryOperations, StorageBackend):
    """Local filesystem storage backend for development."""

    def __init__(self):
        self.base_path = Path(os.getenv("STORAGE_PATH", "./storage"))
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageBackend initialized with base_path: {self.base_path}")

    def _get_full_path(self, storage_key: str, *, directory=False) -> Path:
        key = validate_key(storage_key, directory=directory)
        root = self.base_path.resolve()
        full = root / key
        for parent in [full, *full.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("Storage paths cannot contain symbolic links")
        if not full.resolve().is_relative_to(root):
            raise ValueError("Storage path escapes its root")
        return full

    def resolve_file_path(self, storage_key: str) -> Path:
        """Resolve an existing original file, rejecting traversal and redirected paths."""
        full = self._get_full_path(storage_key)
        if not full.is_file():
            raise FileNotFoundError("文件已移动或删除")
        return full

    def upload(self, file_path: str, storage_key: str) -> str:
        try:
            source = Path(file_path)
            if not source.exists():
                raise StorageError(operation="upload", error=f"Source file not found: {file_path}")
            destination = self._get_full_path(storage_key)
            if os.name == "posix":
                with source.open("rb") as stream:
                    local_paths.write_stream(self.base_path, storage_key, stream)
            else:
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
            if os.name == "posix":
                local_paths.write_stream(self.base_path, storage_key, io.BytesIO(content))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            logger.info(f"Bytes uploaded to local storage: {storage_key}")
            return str(destination)
        except Exception as e:
            logger.error(f"Failed to upload bytes to local storage: {e}")
            raise StorageError(operation="upload", error=str(e))

    def download(self, storage_key: str, local_path: str) -> None:
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.download_bytes(storage_key))

    def download_bytes(self, storage_key: str) -> bytes:
        try:
            source = self._get_full_path(storage_key)
            if not source.exists():
                raise StorageError(operation="download", error=f"File not found: {storage_key}")
            content = local_paths.read_bytes(self.base_path, storage_key) if os.name == "posix" else source.read_bytes()
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
                if os.name == "posix":
                    local_paths.remove_file(self.base_path, storage_key)
                else:
                    file_path.unlink()
                logger.info(f"File deleted from local storage: {storage_key}")
            else:
                logger.warning(f"File not found for deletion: {storage_key}")
        except Exception as e:
            logger.error(f"Failed to delete file from local storage: {e}")
            raise StorageError(operation="delete", error=str(e))

    def exists(self, storage_key: str) -> bool:
        try:
            self._get_full_path(storage_key).stat()
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            raise StorageError(operation="exists", error=type(exc).__name__) from exc

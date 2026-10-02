from types import SimpleNamespace

import pytest
from core.infra.exceptions import StorageError
from core.storage.oss import OSSStorageBackend
from core.storage.s3 import S3StorageBackend


class UnavailableBucket:
    def object_exists(self, key):
        raise RuntimeError("timeout")


def test_oss_timeout_is_not_reported_as_a_missing_object():
    storage = object.__new__(OSSStorageBackend)
    storage.bucket = UnavailableBucket()
    storage.key_prefix = ""
    storage._oss2 = SimpleNamespace(exceptions=SimpleNamespace(OssError=RuntimeError))
    with pytest.raises(StorageError):
        storage.exists("artifacts/file")


class ClientFailure(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class HeadClient:
    def __init__(self, code):
        self.code = code

    def head_object(self, **kwargs):
        raise ClientFailure(self.code)


def s3(code):
    storage = object.__new__(S3StorageBackend)
    storage.s3_client = HeadClient(code)
    storage.bucket = "test"
    storage.ClientError = ClientFailure
    return storage


def test_s3_distinguishes_absence_from_permission_or_server_failure():
    assert s3("404").exists("file") is False
    for code in ("403", "500"):
        with pytest.raises(StorageError):
            s3(code).exists("file")

from pathlib import Path
from core.storage.local import LocalStorageBackend


def test_local_io_failure_is_not_reported_as_a_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    storage = LocalStorageBackend()

    def unavailable(*args, **kwargs):
        raise PermissionError("access denied")

    with monkeypatch.context() as boundary:
        boundary.setattr(Path, "stat", unavailable)
        with pytest.raises(StorageError):
            storage.exists("artifacts/file")

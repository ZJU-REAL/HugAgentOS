"""Storage backends (local, S3 with a fake client), key generation and the factory."""

from unittest.mock import MagicMock

import boto3
import pytest

from core.files.storage import (
    LocalStorageBackend,
    S3StorageBackend,
    generate_storage_key,
    get_storage,
    get_storage_backend,
    get_storage_category_for_resource,
)
from core.infra.exceptions import StorageError
from core.files.storage.factory import process_state


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    return LocalStorageBackend()


@pytest.fixture
def s3_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.delenv("S3_CDN_DOMAIN", raising=False)
    return client


def test_local_bytes_round_trip_and_delete(local):
    key = "test/user_001/file.txt"
    local.upload_bytes(b"content", key)
    assert local.exists(key)
    assert local.download_bytes(key) == b"content"

    local.delete(key)
    assert not local.exists(key)


def test_local_file_upload_and_download(local, tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"from disk")
    local.upload(str(source), "test/user_001/uploaded.txt")

    dest = tmp_path / "out" / "downloaded.txt"
    local.download("test/user_001/uploaded.txt", str(dest))
    assert dest.read_bytes() == b"from disk"


def test_local_presigned_url_is_a_file_url(local):
    local.upload_bytes(b"x", "test/user_001/presigned.txt")
    assert local.generate_presigned_url("test/user_001/presigned.txt").startswith("file://")


def test_local_rejects_path_traversal(local, tmp_path):
    with pytest.raises(StorageError):
        local.upload_bytes(b"x", "../../../etc/passwd")
    assert not (tmp_path / "etc").exists()


def test_local_missing_file_reports_not_found(local):
    with pytest.raises(StorageError) as error:
        local.download_bytes("nonexistent/file.txt")
    assert "not found" in str(error.value.data.get("error", "")).lower()


def test_s3_upload_bytes_puts_object(s3_client):
    S3StorageBackend().upload_bytes(b"s3 content", "test/user_001/s3_file.txt")

    s3_client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="test/user_001/s3_file.txt", Body=b"s3 content"
    )


def test_s3_presigned_url(s3_client):
    s3_client.generate_presigned_url.return_value = "https://test-bucket.s3.amazonaws.com/k?sig=1"

    url = S3StorageBackend().generate_presigned_url("test/user_001/file.txt", expires_in=60)

    assert url == "https://test-bucket.s3.amazonaws.com/k?sig=1"
    assert s3_client.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 60


def test_s3_presigned_url_uses_cdn_domain(s3_client, monkeypatch):
    monkeypatch.setenv("S3_CDN_DOMAIN", "cdn.example.com")
    s3_client.generate_presigned_url.return_value = (
        "https://test-bucket.s3.amazonaws.com/path/file.txt?X-Amz-Signature=abc"
    )

    url = S3StorageBackend().generate_presigned_url("path/file.txt")

    assert url == "https://cdn.example.com/path/file.txt?X-Amz-Signature=abc"


def test_storage_key_layout():
    parts = generate_storage_key(
        env="prod", user_id="user_001", category="artifacts", filename="report.pdf"
    ).split("/")
    assert parts[:3] == ["prod", "artifacts", "user_001"]
    assert parts[3].endswith("_report.pdf")

    with_chat = generate_storage_key(
        env="prod", user_id="user_001", category="artifacts", filename="report.pdf", chat_id="chat_123"
    ).split("/")
    assert len(with_chat) == 5 and with_chat[3] == "chat_123"


@pytest.mark.parametrize("filename", ["../../../etc/passwd", "report (final) [2024].pdf"])
def test_storage_key_sanitizes_filename(filename):
    leaf = generate_storage_key(env="prod", user_id="u", category="artifacts", filename=filename).split("/")[-1]
    assert ".." not in leaf and "/" not in leaf
    assert not set("()[] ") & set(leaf)


def test_category_mapping():
    assert get_storage_category_for_resource("artifact") == "artifacts"
    assert get_storage_category_for_resource("kb_document") == "kb_documents"
    assert get_storage_category_for_resource("upload") == "uploads"
    assert get_storage_category_for_resource("unknown") == "uploads"


def test_factory_selects_backend_by_storage_type(tmp_path, monkeypatch, s3_client):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_TYPE", "local")
    assert isinstance(get_storage_backend(), LocalStorageBackend)

    monkeypatch.setenv("STORAGE_TYPE", "s3")
    assert isinstance(get_storage_backend(), S3StorageBackend)

    monkeypatch.setenv("STORAGE_TYPE", "ftp")
    with pytest.raises(ValueError):
        get_storage_backend()


def test_get_storage_is_a_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setattr(process_state(), "storage_instance", None)

    assert get_storage() is get_storage()

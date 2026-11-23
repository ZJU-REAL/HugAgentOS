"""Raw-byte attachment reads verify ``user_id`` ownership before touching storage.

Attachment ``file_id`` values come from the client (``request.attachments``), so
``download_artifact_bytes`` must refuse another user's artifact instead of
materialising its bytes (image base64) into the agent's context.
"""

from __future__ import annotations

import pytest

from core.files import artifact_bytes
from core.files import storage as storage_mod
from core.files.artifacts import reader
from core.llm.runtime import file_context


@pytest.fixture
def fake_storage(monkeypatch):
    calls = []

    class _Storage:
        def download_bytes(self, key: str) -> bytes:
            calls.append(key)
            return b"artifact-bytes"

    monkeypatch.setattr(storage_mod, "get_storage", lambda: _Storage())
    monkeypatch.setattr(
        reader,
        "resolve_artifact_storage",
        lambda file_id, fallback_name="file": ("storage-key", fallback_name),
    )
    return calls


def _meta_returns(monkeypatch, value):
    seen = []

    def load(file_id, user_id=None):
        seen.append((file_id, user_id))
        return value

    monkeypatch.setattr(reader, "load_artifact_meta", load)
    return seen


def test_foreign_artifact_is_denied_without_storage_access(monkeypatch, fake_storage):
    seen = _meta_returns(monkeypatch, None)

    out = artifact_bytes.download_artifact_bytes(
        "ua_someone_else", "x.png", "test", user_id="attacker"
    )

    assert out is None
    assert seen == [("ua_someone_else", "attacker")]
    assert fake_storage == []


def test_owned_artifact_is_downloaded_once(monkeypatch, fake_storage):
    _meta_returns(monkeypatch, {"file_id": "ua_mine"})

    out = artifact_bytes.download_artifact_bytes("ua_mine", "x.png", "test", user_id="owner")

    assert out == b"artifact-bytes"
    assert fake_storage == ["storage-key"]


def test_no_user_id_keeps_the_trust_the_caller_contract(monkeypatch, fake_storage):
    seen = _meta_returns(monkeypatch, None)

    out = artifact_bytes.download_artifact_bytes("ua_legacy", "x.png", "test")

    assert out == b"artifact-bytes"
    assert seen == []
    assert fake_storage == ["storage-key"]


def test_fetch_image_base64_threads_user_id_to_the_gate(monkeypatch):
    captured = {}

    def download(file_id, fallback_name, log_prefix, user_id=None):
        captured.update(file_id=file_id, user_id=user_id)
        return None

    monkeypatch.setattr(artifact_bytes, "download_artifact_bytes", download)

    out = file_context.fetch_image_base64(
        {"file_id": "ua_img", "name": "a.png", "mime_type": "image/png"}, user_id="owner"
    )

    assert out is None
    assert captured == {"file_id": "ua_img", "user_id": "owner"}

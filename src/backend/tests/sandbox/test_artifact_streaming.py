"""Streaming sandbox artifact export tests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.config.settings import SandboxSettings
from core.sandbox._common import stream_to_file
from core.sandbox.errors import SandboxFileTooLargeError
from core.sandbox.script_runner_provider import ScriptRunnerProvider
from fastapi import HTTPException
from services.script_runner_service import server as runner_server


class _CaptureToolkit:
    def register_tool_function(self, function, *, namesake_strategy: str):
        assert namesake_strategy == "override"
        self.function = function


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def test_sandbox_artifact_limit_defaults_to_100_mib(monkeypatch):
    monkeypatch.delenv("SANDBOX_ARTIFACT_MAX_BYTES", raising=False)
    assert SandboxSettings().artifact_max_bytes == 100 * 1024 * 1024


def test_sandbox_artifact_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("SANDBOX_ARTIFACT_MAX_BYTES", "12345")
    assert SandboxSettings().artifact_max_bytes == 12345


@pytest.mark.asyncio
async def test_stream_to_file_accepts_exact_limit(tmp_path):
    destination = tmp_path / "exact.bin"
    written = await stream_to_file(_chunks(b"1234", b"5678"), destination, max_bytes=8)
    assert written == 8
    assert destination.read_bytes() == b"12345678"


@pytest.mark.asyncio
async def test_stream_to_file_rejects_one_byte_over_and_removes_partial(tmp_path):
    destination = tmp_path / "too-large.bin"
    with pytest.raises(SandboxFileTooLargeError) as exc_info:
        await stream_to_file(_chunks(b"12345678", b"9"), destination, max_bytes=8)
    assert exc_info.value.actual_size == 9
    assert exc_info.value.max_size == 8
    assert not destination.exists()


@pytest.mark.asyncio
async def test_script_runner_raw_endpoint_enforces_limit(monkeypatch, tmp_path):
    within = tmp_path / "within.pdf"
    within.write_bytes(b"12345678")
    over = tmp_path / "over.pdf"
    over.write_bytes(b"123456789")
    monkeypatch.setattr(runner_server, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(runner_server, "MAX_ARTIFACT_EXPORT_BYTES", 8)

    response = await runner_server.get_file_raw(runner_server.GetFileRequest(path=str(within)))
    assert Path(response.path) == within
    assert response.headers["x-artifact-size"] == "8"

    with pytest.raises(HTTPException) as exc_info:
        await runner_server.get_file_raw(runner_server.GetFileRequest(path=str(over)))
    assert exc_info.value.status_code == 413


class _ScriptRunnerResponse:
    status_code = 200
    headers = {"x-artifact-size": "8"}

    async def aread(self) -> bytes:
        return b""

    async def aiter_bytes(self, chunk_size: int):
        assert chunk_size == 1024 * 1024
        yield b"1234"
        yield b"5678"


class _StreamContext:
    async def __aenter__(self):
        return _ScriptRunnerResponse()

    async def __aexit__(self, *args):
        return None


class _ScriptRunnerClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, method: str, url: str, json: dict):
        assert method == "POST"
        assert url.endswith("/get_file_raw")
        assert json == {"path": "/workspace/report.pdf"}
        return _StreamContext()


@pytest.mark.asyncio
async def test_script_runner_streams_file_to_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        _ScriptRunnerClient,
    )
    destination = tmp_path / "script-runner.pdf"
    written = await ScriptRunnerProvider().get_file_to_path(
        None,
        "/workspace/report.pdf",
        destination,
        max_bytes=8,
    )
    assert written == 8
    assert destination.read_bytes() == b"12345678"


class _OversizedScriptRunnerResponse:
    status_code = 413
    headers: dict[str, str] = {}

    async def aread(self) -> bytes:
        return b'{"detail":"file too large: 9 > 8"}'


class _OversizedStreamContext:
    async def __aenter__(self):
        return _OversizedScriptRunnerResponse()

    async def __aexit__(self, *args):
        return None


class _OversizedScriptRunnerClient(_ScriptRunnerClient):
    def stream(self, method: str, url: str, json: dict):
        return _OversizedStreamContext()


@pytest.mark.asyncio
async def test_script_runner_reports_sidecar_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "core.sandbox.script_runner_provider.httpx.AsyncClient",
        _OversizedScriptRunnerClient,
    )
    destination = tmp_path / "too-large.bin"
    with pytest.raises(SandboxFileTooLargeError) as exc_info:
        await ScriptRunnerProvider().get_file_to_path(
            None,
            "/workspace/too-large.bin",
            destination,
            max_bytes=10,
        )
    assert exc_info.value.actual_size == 9
    assert exc_info.value.max_size == 8
    assert not destination.exists()


class _OpenSandboxFiles:
    async def get_file_info(self, paths: list[str]):
        return {paths[0]: SimpleNamespace(size=8)}

    async def read_bytes_stream(self, path: str, *, chunk_size: int):
        assert path == "/workspace/report.pdf"
        assert chunk_size == 1024 * 1024
        return _chunks(b"1234", b"5678")


@pytest.mark.asyncio
async def test_opensandbox_streams_file_to_path(tmp_path):
    OpenSandboxProvider = pytest.importorskip(
        "core.sandbox.opensandbox_provider"
    ).OpenSandboxProvider
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)

    async def _get_session(session_id: str, *, user_id: str | None):
        assert session_id == "chat-1"
        assert user_id == "user-1"
        return SimpleNamespace(sandbox=SimpleNamespace(files=_OpenSandboxFiles()))

    provider._get_or_create_session = _get_session
    destination = tmp_path / "opensandbox.pdf"
    written = await provider.get_file_to_path(
        "chat-1",
        "/workspace/report.pdf",
        destination,
        max_bytes=8,
        user_id="user-1",
    )
    assert written == 8
    assert destination.read_bytes() == b"12345678"


class _CubeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def __aiter__(self):
        return _chunks(b"1234", b"5678")


class _CubeFiles:
    async def get_info(self, path: str, *, request_timeout: int):
        assert path == "/workspace/report.pdf"
        assert request_timeout == 120
        return SimpleNamespace(size=8)

    async def read(self, path: str, **kwargs):
        assert path == "/workspace/report.pdf"
        assert kwargs == {
            "format": "stream",
            "request_timeout": 120,
            "stream_idle_timeout": 120,
        }
        return _CubeStream()


@pytest.mark.asyncio
async def test_cube_streams_file_to_path(tmp_path):
    CubeSandboxProvider = pytest.importorskip("core.sandbox.cube_provider").CubeSandboxProvider
    provider = CubeSandboxProvider.__new__(CubeSandboxProvider)
    provider._request_timeout_s = 120

    @asynccontextmanager
    async def _session_sandbox(session_id: str | None):
        assert session_id == "chat-1"
        yield SimpleNamespace(files=_CubeFiles())

    provider._session_sandbox = _session_sandbox
    destination = tmp_path / "cube.pdf"
    written = await provider.get_file_to_path(
        "chat-1",
        "/workspace/report.pdf",
        destination,
        max_bytes=8,
    )
    assert written == 8
    assert destination.read_bytes() == b"12345678"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["opensandbox", "cube"])
async def test_remote_providers_reject_known_size_before_stream(provider_kind: str, tmp_path: Path):
    destination = tmp_path / f"{provider_kind}.bin"

    if provider_kind == "opensandbox":
        OpenSandboxProvider = pytest.importorskip(
            "core.sandbox.opensandbox_provider"
        ).OpenSandboxProvider
        provider = OpenSandboxProvider.__new__(OpenSandboxProvider)

        class _Files:
            async def get_file_info(self, paths):
                return {paths[0]: SimpleNamespace(size=9)}

            async def read_bytes_stream(self, *args, **kwargs):
                raise AssertionError("oversized file must not be streamed")

        async def _get_session(session_id, *, user_id):
            return SimpleNamespace(sandbox=SimpleNamespace(files=_Files()))

        provider._get_or_create_session = _get_session
        call = provider.get_file_to_path("chat-1", "/workspace/large.bin", destination, max_bytes=8)
    else:
        CubeSandboxProvider = pytest.importorskip("core.sandbox.cube_provider").CubeSandboxProvider
        provider = CubeSandboxProvider.__new__(CubeSandboxProvider)
        provider._request_timeout_s = 120

        class _Files:
            async def get_info(self, *args, **kwargs):
                return SimpleNamespace(size=9)

            async def read(self, *args, **kwargs):
                raise AssertionError("oversized file must not be streamed")

        @asynccontextmanager
        async def _session_sandbox(session_id):
            yield SimpleNamespace(files=_Files())

        provider._session_sandbox = _session_sandbox
        call = provider.get_file_to_path("chat-1", "/workspace/large.bin", destination, max_bytes=8)

    with pytest.raises(SandboxFileTooLargeError) as exc_info:
        await call
    assert exc_info.value.actual_size == 9
    assert not destination.exists()


def test_artifact_store_persists_from_file_without_byte_upload(monkeypatch, tmp_path):
    from core.artifacts import store

    source = tmp_path / "source.pdf"
    source.write_bytes(b"streamed-pdf")
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(store, "_STORE_DIR", artifact_dir)
    monkeypatch.setattr(store, "_INDEX_PATH", artifact_dir / "index.json")
    monkeypatch.setattr(store, "_storage_type", lambda: "local")

    item = store.save_artifact_file(
        file_path=source,
        name="report.pdf",
        mime_type="application/pdf",
        extension="pdf",
    )

    assert item["size"] == len(b"streamed-pdf")
    assert Path(item["path"]).read_bytes() == b"streamed-pdf"
    assert item["storage_key"].endswith(".pdf")


def test_artifact_store_uses_file_upload_for_oss(monkeypatch, tmp_path):
    from core.artifacts import store

    source = tmp_path / "source.pdf"
    source.write_bytes(b"streamed-pdf")
    artifact_dir = tmp_path / "artifacts"
    uploaded_files: list[tuple[str, str]] = []
    uploaded_bytes: list[tuple[bytes, str]] = []

    class _Storage:
        def download_bytes(self, key: str) -> bytes:
            raise FileNotFoundError(key)

        def upload(self, path: str, key: str) -> str:
            uploaded_files.append((path, key))
            return f"oss://bucket/{key}"

        def upload_bytes(self, content: bytes, key: str) -> str:
            uploaded_bytes.append((content, key))
            return f"oss://bucket/{key}"

    storage = _Storage()
    monkeypatch.setattr(store, "_STORE_DIR", artifact_dir)
    monkeypatch.setattr(store, "_INDEX_PATH", artifact_dir / "index.json")
    monkeypatch.setattr(store, "_storage_type", lambda: "oss")
    monkeypatch.setattr(store, "_get_oss_storage", lambda: storage)

    item = store.save_artifact_file(
        file_path=source,
        name="report.pdf",
        mime_type="application/pdf",
        extension="pdf",
    )

    assert uploaded_files == [(str(source), item["storage_key"])]
    assert item["path"] is None
    assert uploaded_bytes and uploaded_bytes[-1][1] == "artifacts/_index.json"
    assert all(content != b"streamed-pdf" for content, _ in uploaded_bytes)


@pytest.mark.asyncio
async def test_sandbox_get_artifact_streams_then_removes_temp_file(monkeypatch):
    import core.sandbox as sandbox_package
    from core.llm.tools import sandbox_tool

    class _Provider:
        async def get_file_to_path(self, session_id, path, destination, *, max_bytes, user_id):
            assert session_id == "chat-1"
            assert path == "/workspace/report.pdf"
            assert max_bytes == 100 * 1024 * 1024
            destination.write_bytes(b"pdf-data")
            return 8

    captured: dict[str, Path] = {}

    def _store(file_path, **kwargs):
        path = Path(file_path)
        assert path.read_bytes() == b"pdf-data"
        captured["temp"] = path
        return {
            "file_id": "fid-report",
            "name": kwargs["name"],
            "url": "/files/fid-report",
            "mime_type": kwargs["mime_type"],
            "size": 8,
            "storage_key": "artifacts/fid-report.pdf",
        }

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr(sandbox_package, "get_sandbox_provider", lambda: _Provider())
    monkeypatch.setattr(sandbox_tool, "_store_generated_file_path", _store)
    toolkit = _CaptureToolkit()
    sandbox_tool.register_sandbox_get_artifact(toolkit, chat_id="chat-1", user_id="user-1")

    response = await toolkit.function("/workspace/report.pdf", "报告.pdf")
    payload = json.loads(response.content[0].text)
    assert payload["ok"] is True
    assert payload["file_id"] == "fid-report"
    assert not captured["temp"].exists()


@pytest.mark.asyncio
async def test_sandbox_get_artifact_returns_structured_oversize_hint(monkeypatch):
    import core.sandbox as sandbox_package
    from core.llm.tools import sandbox_tool

    class _Provider:
        async def get_file_to_path(self, *args, **kwargs):
            raise SandboxFileTooLargeError(
                actual_size=100 * 1024 * 1024 + 1,
                max_size=100 * 1024 * 1024,
            )

    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    monkeypatch.setattr(sandbox_package, "get_sandbox_provider", lambda: _Provider())
    toolkit = _CaptureToolkit()
    sandbox_tool.register_sandbox_get_artifact(toolkit, chat_id="chat-1")

    response = await toolkit.function("/workspace/large.pdf")
    payload = json.loads(response.content[0].text)
    assert payload["code"] == "sandbox_artifact_too_large"
    assert payload["max_size"] == 100 * 1024 * 1024
    assert "PDF" in payload["suggestion"]

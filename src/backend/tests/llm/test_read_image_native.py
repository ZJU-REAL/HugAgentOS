"""多模态主模型直接看图：``Read`` 返回图片块、原生版 ``read_image``、看图方式判定。"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from agentscope.message import DataBlock, TextBlock

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Toolkit:
    def __init__(self):
        self.fns = {}

    def register_tool_function(self, fn, **kw):
        self.fns[fn.__name__] = fn


def _image_response_parts(response):
    return json.loads(response.content[0].text), response.content[1]


def _error(response):
    assert len(response.content) == 1
    return json.loads(response.content[0].text)["error"]


# ── resp_image ────────────────────────────────────────────────────────────


def test_resp_image_carries_pixels_as_data_block():
    from core.llm.tools._common import resp_image

    resp = asyncio.run(
        resp_image(
            PNG_1X1, "image/png", name="/workspace/a.png", meta={"file_path": "/workspace/a.png"}
        )
    )
    meta, block = _image_response_parts(resp)
    assert isinstance(resp.content[0], TextBlock)
    assert isinstance(block, DataBlock)
    assert meta == {
        "type": "image",
        "mime_type": "image/png",
        "size": len(PNG_1X1),
        "file_path": "/workspace/a.png",
    }
    assert block.source.media_type == "image/png"
    assert base64.b64decode(block.source.data) == PNG_1X1
    assert block.name == "/workspace/a.png"


# ── 原生版 read_image ──────────────────────────────────────────────────────


def _register_native_read_image():
    from core.llm.tools.read_image_tool import register_read_image

    tk = _Toolkit()
    register_read_image(tk, chat_id="c1", user_id="u1", vision_mode="native")
    return tk.fns["read_image"]


def test_read_image_returns_pixels_by_file_id(monkeypatch):
    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    fn = _register_native_read_image()
    meta, block = _image_response_parts(asyncio.run(fn(file_id="art-1")))
    assert meta == {
        "type": "image",
        "source": "art-1",
        "mime_type": "image/png",
        "size": len(PNG_1X1),
    }
    assert base64.b64decode(block.source.data) == PNG_1X1


def test_read_image_returns_pixels_by_file_path(monkeypatch):
    from core.llm.tools import read_image_tool

    async def _fake_read(file_path, session, user_id, scope=None):
        return PNG_1X1

    monkeypatch.setattr(read_image_tool, "_read_path_bytes", _fake_read)
    fn = _register_native_read_image()
    meta, block = _image_response_parts(asyncio.run(fn(file_path="/workspace/shot.png")))
    assert meta["source"] == "/workspace/shot.png"
    assert block.source.media_type == "image/png"


def test_native_read_image_has_no_focus_argument():
    import inspect

    assert "focus" not in inspect.signature(_register_native_read_image()).parameters


def test_read_image_requires_a_target():
    assert "file_path 或 file_id" in _error(asyncio.run(_register_native_read_image()()))


def test_read_image_rejects_non_image_bytes(monkeypatch):
    from core.llm import hooks

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: b"PK\x03\x04zip")
    assert "不是可识别的图片格式" in _error(
        asyncio.run(_register_native_read_image()(file_id="art-1"))
    )


def test_read_image_rejects_paths_outside_workspace():
    resp = asyncio.run(_register_native_read_image()(file_path="/etc/passwd"))
    assert "error" in json.loads(resp.content[0].text)


def test_read_image_refuses_oversized_image(monkeypatch):
    from core.llm import hooks
    from core.vision import service as svc

    monkeypatch.setattr(hooks, "_download_artifact_bytes", lambda *a, **kw: PNG_1X1)
    monkeypatch.setattr(svc, "MAX_IMAGE_BYTES", len(PNG_1X1) - 1)
    assert "图片过大" in _error(asyncio.run(_register_native_read_image()(file_id="art-1")))


def test_read_image_not_registered_without_any_vision():
    from core.llm.tools.read_image_tool import register_read_image

    tk = _Toolkit()
    register_read_image(tk, chat_id="c1", user_id="u1", vision_mode="none")
    assert tk.fns == {}


# ── Read 的图片分支按看图方式分叉 ─────────────────────────────────────────


def _register_read(vision_mode, monkeypatch):
    from core.llm.tools import read_tool
    from core.llm.tools._state import ReadStateTracker

    class _Provider:
        async def get_file(self, session, physical, user_id=None):
            return PNG_1X1

    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: _Provider())
    tk = _Toolkit()
    read_tool.register_read(
        tk, chat_id="c1", user_id="u1", state=ReadStateTracker(), vision_mode=vision_mode
    )
    return tk.fns["Read"]


def test_read_returns_pixels_for_native_vision_model(monkeypatch):
    Read = _register_read("native", monkeypatch)
    meta, block = _image_response_parts(asyncio.run(Read(file_path="/workspace/shot.png")))
    assert meta["type"] == "image"
    assert meta["file_path"] == "/workspace/shot.png"
    assert isinstance(block, DataBlock)
    assert "直接以图片返回" in Read.__doc__


def test_read_transcribes_for_bridge_mode(monkeypatch):
    from core.llm.tools import read_tool

    async def _fake_evidence(content_bytes, file_path):
        return {"type": "image_evidence", "file_path": file_path, "evidence": "一张图"}

    monkeypatch.setattr(read_tool, "_read_image_as_evidence", _fake_evidence)
    Read = _register_read("bridge", monkeypatch)
    resp = asyncio.run(Read(file_path="/workspace/shot.png"))
    assert len(resp.content) == 1
    assert json.loads(resp.content[0].text)["type"] == "image_evidence"
    assert "read_image(file_path=..., focus=...)" in Read.__doc__


def test_read_falls_back_to_binary_without_vision(monkeypatch):
    Read = _register_read("none", monkeypatch)
    resp = asyncio.run(Read(file_path="/workspace/shot.png"))
    assert len(resp.content) == 1
    assert json.loads(resp.content[0].text)["type"] == "binary"
    assert "read_image" not in Read.__doc__


# ── 看图方式判定：按本次实际用的模型 ─────────────────────────────────────


def _model_service(monkeypatch, *, main_vision: bool, provider_vision=None):
    from core.services.model_config import ModelConfigService

    class _Service:
        @staticmethod
        def resolve(role):
            if role != "main_agent":
                return None
            return SimpleNamespace(extra={"supports_vision": main_vision})

        @staticmethod
        def resolve_provider(pid):
            if provider_vision is None:
                return None
            return SimpleNamespace(extra={"supports_vision": provider_vision})

    monkeypatch.setattr(ModelConfigService, "get_instance", staticmethod(lambda: _Service()))


def test_vision_mode_prefers_selected_provider(monkeypatch):
    from core.vision import resolve_vision_mode

    _model_service(monkeypatch, main_vision=False, provider_vision=True)
    assert resolve_vision_mode("prov-1") == "native"
    assert resolve_vision_mode("") == "none"


def test_vision_mode_probe_failure_means_none(monkeypatch):
    from core.services.model_config import ModelConfigService
    from core.vision import resolve_vision_mode

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ModelConfigService, "get_instance", staticmethod(_boom))
    assert resolve_vision_mode("") == "none"


def test_read_image_is_governed_by_local_path_permission():
    from core.llm.tool_permissions import builtin_tool_permission

    spec = builtin_tool_permission("read_image")
    assert spec is not None
    assert "file_path:read" in spec.key


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

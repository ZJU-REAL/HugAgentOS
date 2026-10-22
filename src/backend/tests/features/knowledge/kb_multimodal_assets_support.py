"""知识库多模态（路径 A：图生文入索引）的单元测试。

盯的是那几处「错了不会报错、只会悄悄变差或变脏」的地方：

* 图片上传曾被当纯文本 latin-1 硬解成乱码入库——静默污染向量库，必须回归；
* 富解析的开关必须是**显式**传给解析服务的，漏传就永远拿不到图，且没有任何报错；
* 图片字节是 ``data:image/...;base64,`` 数据 URI，少剥一层前缀就存进一堆坏文件；
* 资产必须落在**包含它的父块**上——挂错块，检索命中后回溯到的是别处的正文；
* 占位符改写必须同时改父块和子块，否则入库正文与被向量化的文本不一致；
* 未配置视觉模型是正常状态，不能因此让整篇文档索引失败；
* KB 的批量图像理解不许占满进程级的视觉桥信号量，否则对话看图会被排到后面。
"""


from __future__ import annotations
from core.files.content import pdf_parser
from core.files.content import parser_config


from core.models import embedding as vector_embedding


from features.knowledge import vector_indexing


from features.knowledge import parser_extract


from features.knowledge import parser_types


from features.knowledge import asset_captions


from features.knowledge import asset_indexing


from features.knowledge import kb_assets


from features.knowledge import kb_assets_state


import base64


import json


from types import SimpleNamespace


import pytest


from core.files.content import file_parser


from features.knowledge import kb_assets, kb_parser


_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # pragma: no cover - always OK in these tests
        return None

    def json(self) -> dict:
        return self._payload


def _install_fake_parser(monkeypatch, payload: dict) -> dict:
    """Stub the parse service; returns a dict that captures the posted form data."""
    captured: dict = {}

    def _fake_post(url, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["filename"] = files["files"][0] if files else None
        return _FakeResponse(payload)

    monkeypatch.setattr(parser_config, 'cfg_api_url', lambda: "http://parser.test/file_parse")
    monkeypatch.setattr(pdf_parser.requests, "post", _fake_post)
    return captured


_IMG_NAME = "abc123.jpg"


_RICH_PAYLOAD = {
    "results": {
        "doc": {
            "md_content": f"# 标题\n\n![](images/{_IMG_NAME})\n\n图 1 季度营收趋势\n\n结论段落。",
            "images": {
                _IMG_NAME: "data:image/jpeg;base64," + base64.b64encode(b"JPEGBYTES").decode()
            },
            "content_list": json.dumps(
                [
                    {
                        "type": "image",
                        "img_path": f"images/{_IMG_NAME}",
                        "image_caption": ["图 1 季度营收趋势"],
                        "image_footnote": [],
                        "bbox": [139, 178, 647, 431],
                        "page_idx": 3,
                    }
                ]
            ),
        }
    }
}


class _FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload_bytes(self, content: bytes, storage_key: str) -> str:
        self.objects[storage_key] = content
        return f"memory://{storage_key}"

    def download_bytes(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def delete(self, storage_key: str) -> None:
        self.deleted.append(storage_key)
        self.objects.pop(storage_key, None)


class _FakeSession:
    """Just enough Session surface for persist_assets/generate_captions."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, _obj) -> None:
        return None


@pytest.fixture()
def fake_storage(monkeypatch):
    storage = _FakeStorage()
    monkeypatch.setattr('core.files.storage.get_storage', lambda: storage)
    return storage


def _parent(parent_id: str, content: str):
    child = parser_types.ChildChunk(child_id=f"{parent_id}_0", content=content, index=0)
    return parser_types.ParentChunk(parent_id=parent_id, content=content, children=[child])


def _asset_row(**kwargs):
    base = dict(
        asset_id="a1",
        kind="image",
        mime_type="image/jpeg",
        storage_key="k1",
        caption=None,
        text_content="图 1 季度营收趋势",
        caption_status="pending",
        chunk_id="p1",
        asset_index=0,
        extra_data={},
        vector_state={},
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _evidence(summary="柱状图：四个季度营收，Q4 最高", ocr="Q1 Q2 Q3 Q4 营收(亿元)"):
    """构造一份视觉桥证据（只用到 kb 侧真正读的字段）。"""
    return SimpleNamespace(
        summary=summary,
        ocr=SimpleNamespace(full_text=ocr),
        semantics=SimpleNamespace(
            scene="财务图表",
            intent="展示季度营收趋势",
            entities=[SimpleNamespace(model_dump=lambda **_k: {"name": "Q4", "type": "季度"})],
            relations=[],
        ),
        uncertainty=["坐标轴单位模糊"],
    )


def _vision_result(evidence=None):
    return SimpleNamespace(
        evidence=evidence if evidence is not None else _evidence(),
        to_meta=lambda: {"model": "qwen-vl", "cached": False},
    )


def _install_bridge(monkeypatch, results, *, available=True, calls=None):
    """替换视觉桥：results 为按批返回的结果列表（长度与该批输入一致）。"""
    monkeypatch.setattr(asset_captions, 'vision_available', lambda: available)

    def _fake_describe(images, loop=None):
        if calls is not None:
            calls.append(len(images))
        return [results.pop(0) if results else None for _ in images]

    monkeypatch.setattr(asset_captions, 'describe_batch', _fake_describe)


def _stub_bridge(monkeypatch, seen: dict):
    """替换 core.models.vision.get_vision_bridge，记录实际调用参数。"""
    import sys
    import types

    async def _describe_many(images, *, focus=None):
        seen["describe_many"] = {"n": len(images), "focus": focus}
        return [f"ok-{i}" for i in range(len(images))]

    async def _describe(data, mime, *, focus=None, use_cache=True):
        seen.setdefault("describe", []).append({"use_cache": use_cache, "focus": focus})
        return f"ok-{mime}"

    bridge = SimpleNamespace(describe_many=_describe_many, describe=_describe)
    module = types.ModuleType("core.models.vision")
    module.get_vision_bridge = lambda: bridge
    module.is_available = lambda: True
    monkeypatch.setitem(sys.modules, "core.models.vision", module)

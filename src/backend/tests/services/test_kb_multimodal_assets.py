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

import base64
import json
from types import SimpleNamespace

import pytest
from core.content import file_parser
from core.kb import kb_assets, kb_parser

# ── 富解析：参数与解码 ────────────────────────────────────────────────────────


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

    monkeypatch.setattr(file_parser, "_cfg_api_url", lambda: "http://parser.test/file_parse")
    monkeypatch.setattr(file_parser.requests, "post", _fake_post)
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


def test_plain_parse_does_not_request_media(monkeypatch):
    """纯文本路径（对话附件）不能顺手把图片一起要回来：图片是 base64 内联在响应体里的，
    一篇图多的 PDF 会凭空多出几 MB 传输，而这条路径根本用不到。"""
    captured = _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    file_parser.parse_pdf(b"%PDF-1.4", "a.pdf")

    assert "return_images" not in captured["data"]
    assert "return_content_list" not in captured["data"]


def test_rich_parse_requests_media_and_decodes_data_uri(monkeypatch):
    captured = _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    result = file_parser.parse_pdf_rich(b"%PDF-1.4", "a.pdf")

    assert captured["data"]["return_images"] == "true"
    assert captured["data"]["return_content_list"] == "true"
    # 数据 URI 前缀必须剥掉，否则存下来的是坏文件
    assert result.images[f"images/{_IMG_NAME}"] == b"JPEGBYTES"
    # 裸文件名也要能查到：markdown 里带 images/ 前缀，images 字典按裸名索引
    assert result.images[_IMG_NAME] == b"JPEGBYTES"
    # content_list 是 JSON **字符串**，不是数组
    assert result.blocks[0]["img_path"] == f"images/{_IMG_NAME}"


def test_rich_parse_survives_undecodable_image(monkeypatch):
    payload = json.loads(json.dumps(_RICH_PAYLOAD))
    payload["results"]["doc"]["images"] = {_IMG_NAME: "data:image/jpeg;base64,!!!not-base64!!!"}
    _install_fake_parser(monkeypatch, payload)

    result = file_parser.parse_pdf_rich(b"%PDF-1.4", "a.pdf")

    # 单张图坏掉不该让整篇文档解析失败——正文照常返回
    assert "标题" in result.markdown


# ── 解析：图片上传与资产抽取 ──────────────────────────────────────────────────


def test_uploaded_image_no_longer_indexed_as_binary_garbage():
    """上传白名单放行 png/jpg/webp/gif，但解析侧原先没有图片分支，
    会落到 latin-1 兜底把二进制解成乱码段落并向量化——静默污染，必须挡住。"""
    paragraphs, assets = kb_parser.extract_text_with_assets(_PNG_1PX, "image/png")

    joined = "".join(p["text"] for p in paragraphs)
    assert "\x89" not in joined and "PNG" not in joined
    assert len(assets) == 1
    assert assets[0].kind == "image"
    assert assets[0].data == _PNG_1PX
    # 占位符必须真的出现在段落里，否则后面定位不到父块
    assert assets[0].placeholder in joined


def test_pdf_assets_carry_caption_and_locator(monkeypatch):
    _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    paragraphs, assets = kb_parser.extract_text_with_assets(b"%PDF-1.4", "application/pdf")

    assert [p["text"] for p in paragraphs][0].startswith("# 标题")
    assert len(assets) == 1
    asset = assets[0]
    assert asset.text_content == "图 1 季度营收趋势"
    assert asset.locator == {"page_idx": 3, "bbox": [139, 178, 647, 431]}
    assert asset.mime_type == "image/jpeg"


def test_rich_parse_failure_degrades_to_text(monkeypatch):
    """解析服务不支持这些参数（或直接抽风）时，必须退回今天的纯文本行为，
    而不是让整篇文档索引失败。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(file_parser, "parse_pdf_rich", _boom)
    monkeypatch.setattr(file_parser, "parse_file", lambda *_a, **_k: "# 纯文本兜底")

    paragraphs, assets = kb_parser.extract_text_with_assets(b"%PDF-1.4", "application/pdf")

    assert assets == []
    assert "纯文本兜底" in "".join(p["text"] for p in paragraphs)


def test_with_assets_false_keeps_legacy_path(monkeypatch):
    """关掉多模态时不能再走重解析——parse_pdf_rich 一次都不该被调到。"""
    calls: list = []

    def _record(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("rich parse must not run when assets are disabled")

    monkeypatch.setattr(file_parser, "parse_pdf_rich", _record)
    monkeypatch.setattr(file_parser, "parse_file", lambda *_a, **_k: "# 正文")

    paragraphs, assets = kb_parser.extract_text_with_assets(
        b"%PDF-1.4", "application/pdf", with_assets=False
    )

    assert calls == []
    assert assets == []
    assert paragraphs


# ── 落库：父块定位与占位符改写 ────────────────────────────────────────────────


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
    monkeypatch.setattr("core.storage.get_storage", lambda: storage)
    return storage


def _parent(parent_id: str, content: str):
    child = kb_parser.ChildChunk(child_id=f"{parent_id}_0", content=content, index=0)
    return kb_parser.ParentChunk(parent_id=parent_id, content=content, children=[child])


def test_asset_lands_on_the_chunk_that_contains_it(fake_storage, monkeypatch):
    """挂错父块的后果是检索命中后回溯到别处的正文——比丢图严重得多。"""
    monkeypatch.setattr(kb_assets, "_MIN_ASSET_BYTES", 0)
    parents = [
        _parent("p_first", "第一段没有图。"),
        _parent("p_second", "第二段：![](images/x.jpg) 见上图。"),
    ]
    assets = [
        kb_parser.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
    ]
    db = _FakeSession()

    created = kb_assets.persist_assets(
        db, kb_id="kb1", document_id="doc1", user_id="u1", assets=assets, parents=parents
    )

    assert len(created) == 1
    assert created[0].chunk_id == "p_second"
    assert fake_storage.objects  # 字节确实落盘了


def test_placeholder_rewritten_in_both_parent_and_child(fake_storage, monkeypatch):
    """入库正文与被向量化的子块文本必须是同一份；只改父块会让两者分叉。"""
    monkeypatch.setattr(kb_assets, "_MIN_ASSET_BYTES", 0)
    parents = [_parent("p1", "见 ![](images/x.jpg) 图。")]
    assets = [
        kb_parser.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
    ]
    db = _FakeSession()

    created = kb_assets.persist_assets(
        db, kb_id="kb1", document_id="doc1", user_id="u1", assets=assets, parents=parents
    )

    url = kb_assets.asset_public_url(created[0].asset_id)
    assert url in parents[0].content
    assert url in parents[0].children[0].content
    assert "images/x.jpg" not in parents[0].content


def test_tiny_assets_are_dropped(fake_storage):
    """版面切出来的分割线/项目符号图标不值一次 VLM 调用，也不该进检索。"""
    parents = [_parent("p1", "![](images/tiny.jpg)")]
    assets = [
        kb_parser.RawAsset(data=b"tiny", placeholder="images/tiny.jpg", mime_type="image/jpeg")
    ]

    created = kb_assets.persist_assets(
        _FakeSession(), kb_id="kb1", document_id="d1", user_id="u1", assets=assets, parents=parents
    )

    assert created == []
    assert fake_storage.objects == {}


def test_storage_failure_skips_one_asset_not_the_document(monkeypatch):
    monkeypatch.setattr(kb_assets, "_MIN_ASSET_BYTES", 0)

    class _BrokenStorage(_FakeStorage):
        def upload_bytes(self, content, storage_key):
            raise RuntimeError("bucket on fire")

    monkeypatch.setattr("core.storage.get_storage", lambda: _BrokenStorage())
    parents = [_parent("p1", "![](images/x.jpg)")]
    assets = [
        kb_parser.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
    ]

    created = kb_assets.persist_assets(
        _FakeSession(), kb_id="kb1", document_id="d1", user_id="u1", assets=assets, parents=parents
    )

    assert created == []
    assert "images/x.jpg" in parents[0].content  # 没落盘就不该改写成死链


# ── 图生文：走视觉桥 ─────────────────────────────────────────────────────────


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
    monkeypatch.setattr(kb_assets, "vision_available", lambda: available)

    def _fake_describe(images, loop=None):
        if calls is not None:
            calls.append(len(images))
        return [results.pop(0) if results else None for _ in images]

    monkeypatch.setattr(kb_assets, "_describe_batch", _fake_describe)


def test_no_vision_model_is_a_degradation_not_a_failure(monkeypatch):
    """未指派 vision 角色、主模型也不识图，是正常状态：图注照常入索引。
    这正是「视觉模型可以晚点再配」的前提——之后回填不用重新解析文档。"""
    monkeypatch.setattr(kb_assets, "vision_available", lambda: False)
    rows = [_asset_row()]
    db = _FakeSession()

    done = kb_assets.generate_captions(db, rows)

    assert done == 0
    assert rows[0].caption_status == "skipped"
    assert "季度营收趋势" in kb_assets.asset_index_text(rows[0], title="年报")


def test_evidence_lands_in_caption_and_ocr_merges_into_text(fake_storage, monkeypatch):
    """证据要落到正确的字段上：概述进 caption，OCR 并进 text_content（与图注共存）。
    并错了检索文本就废了——图注是作者写的用途，OCR 是图里实际的字，两者都要。"""
    fake_storage.objects["k1"] = b"x" * 100
    _install_bridge(monkeypatch, [_vision_result()])
    rows = [_asset_row()]

    done = kb_assets.generate_captions(_FakeSession(), rows)

    assert done == 1
    assert rows[0].caption_status == "completed"
    assert rows[0].caption == "柱状图：四个季度营收，Q4 最高"
    assert "图 1 季度营收趋势" in rows[0].text_content  # 原图注保留
    assert "Q1 Q2 Q3 Q4" in rows[0].text_content       # OCR 并入
    # 实体/不确定项留档，给后续本体层与人工复核用
    assert rows[0].extra_data["vision"]["entities"][0]["name"] == "Q4"
    assert rows[0].extra_data["vision"]["uncertainty"] == ["坐标轴单位模糊"]
    assert rows[0].extra_data["vision"]["meta"]["model"] == "qwen-vl"


def test_ocr_identical_to_caption_is_not_duplicated(fake_storage, monkeypatch):
    fake_storage.objects["k1"] = b"x" * 100
    _install_bridge(monkeypatch, [_vision_result(_evidence(ocr="图 1 季度营收趋势"))])
    rows = [_asset_row()]

    kb_assets.generate_captions(_FakeSession(), rows)

    assert rows[0].text_content == "图 1 季度营收趋势"


def test_text_content_is_capped(fake_storage, monkeypatch):
    """满是文字的扫描件能吐出几万字，直接入库会把行和向量文本一起撑爆。"""
    fake_storage.objects["k1"] = b"x" * 100
    monkeypatch.setattr(kb_assets, "_MAX_TEXT_CONTENT_CHARS", 50)
    _install_bridge(monkeypatch, [_vision_result(_evidence(ocr="字" * 500))])
    rows = [_asset_row()]

    kb_assets.generate_captions(_FakeSession(), rows)

    assert len(rows[0].text_content) == 50


def test_single_image_failure_keeps_asset_indexable(fake_storage, monkeypatch):
    """视觉桥对单张返回 None（任何失败都这样降级），资产仍要凭图注可检索。"""
    fake_storage.objects["k1"] = b"x" * 100
    _install_bridge(monkeypatch, [None])
    rows = [_asset_row()]

    done = kb_assets.generate_captions(_FakeSession(), rows)

    assert done == 0
    assert rows[0].caption_status == "failed"
    assert kb_assets.asset_index_text(rows[0]).strip() != ""


def test_whole_batch_exception_does_not_fail_the_document(fake_storage, monkeypatch):
    fake_storage.objects["k1"] = b"x" * 100
    monkeypatch.setattr(kb_assets, "vision_available", lambda: True)

    def _boom(_images, _loop=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(kb_assets, "_describe_batch", _boom)
    rows = [_asset_row()]

    assert kb_assets.generate_captions(_FakeSession(), rows) == 0
    assert rows[0].caption_status == "failed"


def test_kb_never_saturates_the_shared_vision_lane(fake_storage, monkeypatch):
    """视觉桥的并发信号量是进程级的：一篇图海文档一次性 gather 过去会把对话里的看图
    请求全排到后面。KB 必须按小批提交，任何时刻只占用其中几格。"""
    for i in range(7):
        fake_storage.objects[f"k{i}"] = b"x" * 100
    calls: list = []
    _install_bridge(monkeypatch, [], calls=calls)
    monkeypatch.setattr(kb_assets, "_CAPTION_BATCH", 2)
    rows = [_asset_row(asset_id=f"a{i}", storage_key=f"k{i}") for i in range(7)]

    kb_assets.generate_captions(_FakeSession(), rows)

    assert calls == [2, 2, 2, 1], f"批次切分不对: {calls}"
    assert max(calls) <= 2


def test_unreadable_asset_is_skipped_without_calling_vision(fake_storage, monkeypatch):
    """存储里读不到的那张不该占用一次视觉调用，也不该拖垮同批其他图。"""
    fake_storage.objects["k2"] = b"x" * 100
    calls: list = []
    _install_bridge(monkeypatch, [_vision_result()], calls=calls)
    monkeypatch.setattr(kb_assets, "_CAPTION_BATCH", 5)
    rows = [_asset_row(asset_id="a1", storage_key="missing"), _asset_row(asset_id="a2", storage_key="k2")]

    done = kb_assets.generate_captions(_FakeSession(), rows)

    assert calls == [1]  # 只送了能读出来的那张
    assert rows[0].caption_status == "failed"
    assert done == 1


def test_per_document_cap_marks_the_rest_explicitly(fake_storage, monkeypatch):
    """上限生效要在数据里看得见，否则和"视觉模型什么都没返回"分不开。"""
    for i in range(4):
        fake_storage.objects[f"k{i}"] = b"x" * 100
    _install_bridge(monkeypatch, [])
    monkeypatch.setattr(kb_assets, "_MAX_CAPTIONS_PER_DOC", 2)
    rows = [_asset_row(asset_id=f"a{i}", storage_key=f"k{i}") for i in range(4)]

    kb_assets.generate_captions(_FakeSession(), rows)

    assert [r.caption_status for r in rows[2:]] == ["skipped", "skipped"]


def test_index_text_prefers_caption_then_caption_text():
    row = _asset_row(caption="柱状图，Q4 最高，同比增长 32%")
    text = kb_assets.asset_index_text(row, title="2026 年报")
    assert text.splitlines() == ["柱状图，Q4 最高，同比增长 32%", "图 1 季度营收趋势", "2026 年报"]


# ── 检索行 ───────────────────────────────────────────────────────────────────


def test_asset_rows_point_at_their_parent_chunk(monkeypatch):
    """图片行必须挂在父块上：这样去重、回 PG 取父块正文、重排全都不用改。"""
    written: list[dict] = []
    monkeypatch.setattr(kb_assets, "asset_index_text", lambda a, title="": "柱状图描述")
    import core.kb.kb_vector as kb_vector

    monkeypatch.setattr(kb_vector, "embed_batch", lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr(kb_vector, "upsert_rows", lambda rows: written.extend(rows))

    count = kb_assets.index_assets(
        [_asset_row()], user_id="u1", kb_id="kb1", document_id="d1", title="年报"
    )

    assert count == 1
    row = written[0]
    assert row["row_type"] == "image"
    assert row["parent_chunk_id"] == "p1"
    assert row["chunk_id"] != "p1"  # 主键不能和分块行撞


def test_orphan_asset_falls_back_to_itself(monkeypatch):
    written: list[dict] = []
    import core.kb.kb_vector as kb_vector

    monkeypatch.setattr(kb_vector, "embed_batch", lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr(kb_vector, "upsert_rows", lambda rows: written.extend(rows))

    kb_assets.index_assets(
        [_asset_row(chunk_id=None)], user_id="u1", kb_id="kb1", document_id="d1", title="年报"
    )

    # 没有归属分块时指向自己，检索侧取不到父块正文会退回本行 content（描述本身）
    assert written[0]["parent_chunk_id"] == written[0]["chunk_id"]


def test_multimodal_toggle_precedence(monkeypatch):
    monkeypatch.delenv("KB_MULTIMODAL_INDEXING", raising=False)
    assert kb_assets.multimodal_indexing_enabled(None) is True
    assert kb_assets.multimodal_indexing_enabled({"multimodal_indexing": False}) is False
    monkeypatch.setenv("KB_MULTIMODAL_INDEXING", "false")
    assert kb_assets.multimodal_indexing_enabled(None) is False
    # 单库显式打开要能盖过全局关闭
    assert kb_assets.multimodal_indexing_enabled({"multimodal_indexing": True}) is True


# ── 异步接缝：把协程交回宿主循环 ──────────────────────────────────────────────


def _stub_bridge(monkeypatch, seen: dict):
    """替换 core.vision.get_vision_bridge，记录实际调用参数。"""
    import sys
    import types

    async def _describe_many(images, *, focus=None):
        seen["describe_many"] = {"n": len(images), "focus": focus}
        return [f"ok-{i}" for i in range(len(images))]

    async def _describe(data, mime, *, focus=None, use_cache=True):
        seen.setdefault("describe", []).append({"use_cache": use_cache, "focus": focus})
        return f"ok-{mime}"

    bridge = SimpleNamespace(describe_many=_describe_many, describe=_describe)
    module = types.ModuleType("core.vision")
    module.get_vision_bridge = lambda: bridge
    module.is_available = lambda: True
    monkeypatch.setitem(sys.modules, "core.vision", module)


def test_describe_batch_runs_on_the_host_loop_when_there_is_one(monkeypatch):
    """索引跑在 anyio 工作线程里时，协程必须交回宿主循环执行——只有走宿主循环，
    视觉桥的证据缓存与并发信号量才真的生效（也才不会各起各的循环）。"""
    import anyio
    import anyio.to_thread

    seen: dict = {}
    _stub_bridge(monkeypatch, seen)

    async def _main():
        return await anyio.to_thread.run_sync(
            lambda: kb_assets._describe_batch([(b"a", "image/png"), (b"b", "image/jpeg")])
        )

    results = anyio.run(_main)

    assert results == ["ok-0", "ok-1"]
    assert seen["describe_many"]["n"] == 2
    assert "文档中的插图" in seen["describe_many"]["focus"]
    assert "describe" not in seen


def test_describe_batch_without_host_loop_disables_the_evidence_cache(monkeypatch):
    """没有宿主循环时才起临时循环，而且必须关掉缓存：Redis 连接池是模块级全局、
    绑定首次使用它的循环，让一个转瞬即逝的临时循环抢先创建它，会把整个进程的 Redis
    绑死在已销毁的循环上——比「这次没吃到缓存」严重得多。"""
    seen: dict = {}
    _stub_bridge(monkeypatch, seen)

    results = kb_assets._describe_batch([(b"a", "image/png")])

    assert results == ["ok-image/png"]
    assert seen["describe"][0]["use_cache"] is False
    assert "describe_many" not in seen


def test_explicit_loop_wins_over_anyio(monkeypatch):
    """索引现在跑在常驻 worker **自己的**线程池里（不是 anyio 的），线程里既没有运行中的
    循环、也没有回宿主循环的通路——所以 worker 会把调度它的循环显式传下来，必须优先用它。
    退化成临时循环的话，证据缓存就白配了。"""
    import asyncio
    import threading

    seen: dict = {}
    _stub_bridge(monkeypatch, seen)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        # 在一个普通线程里调用（模拟 worker 的线程池），只有显式 loop 能救它
        box: dict = {}
        worker = threading.Thread(
            target=lambda: box.update(
                r=kb_assets._describe_batch([(b"a", "image/png")], loop)
            )
        )
        worker.start()
        worker.join(timeout=10)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    assert box["r"] == ["ok-0"]
    assert seen["describe_many"]["n"] == 1
    assert "describe" not in seen  # 没退化到不带缓存的临时循环


# ── 检索结果取正文：纯图片父块 ────────────────────────────────────────────────


def test_media_only_parent_falls_back_to_the_asset_row_content():
    """单独上传的图片，其父块正文就是一条 markdown 图片链接。父块无条件覆盖的话，
    调用方拿到的只是个 URL，而命中的图片行里恰好装着描述与转写。"""
    from mcp_servers.retrieve_dataset_content_mcp.local_impl import _pick_content

    link = "![](/api/v1/catalog/kb/assets/abc)"
    rich = "柱状图：Q4 达到峰值 42\nQuarterly Revenue 2026"

    assert _pick_content(link, rich) == rich
    assert _pick_content(f"  {link}  \n", rich) == rich
    # 正常文档：父块里除了图还有正文，必须原样保留父块
    normal = f"第四条 专项资金用于……\n{link}\n见上图。"
    assert _pick_content(normal, rich) == normal
    # 两边都空时不要凭空造内容
    assert _pick_content("", "") == ""
    assert _pick_content(link, "") == link

from core.files.content import pdf_parser
from tests.features.knowledge.kb_multimodal_assets_support import (
    _FakeSession,
    _FakeStorage,
    _IMG_NAME,
    _PNG_1PX,
    _RICH_PAYLOAD,
    _asset_row,
    _evidence,
    _install_bridge,
    _install_fake_parser,
    _parent,
    _vision_result,
    asset_captions,
    asset_indexing,
    fake_storage,
    file_parser,
    json,
    kb_assets,
    kb_assets_state,
    parser_extract,
    parser_types,
    vector_embedding,
    vector_indexing,
)

def test_plain_parse_does_not_request_media(monkeypatch):
    """纯文本路径（对话附件）不能顺手把图片一起要回来：图片是 base64 内联在响应体里的，
    一篇图多的 PDF 会凭空多出几 MB 传输，而这条路径根本用不到。"""
    captured = _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    pdf_parser.parse_pdf(b"%PDF-1.4", "a.pdf")

    assert "return_images" not in captured["data"]
    assert "return_content_list" not in captured["data"]


def test_rich_parse_requests_media_and_decodes_data_uri(monkeypatch):
    captured = _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    result = pdf_parser.parse_pdf_rich(b"%PDF-1.4", "a.pdf")

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

    result = pdf_parser.parse_pdf_rich(b"%PDF-1.4", "a.pdf")

    # 单张图坏掉不该让整篇文档解析失败——正文照常返回
    assert "标题" in result.markdown


def test_uploaded_image_no_longer_indexed_as_binary_garbage():
    """上传白名单放行 png/jpg/webp/gif，但解析侧原先没有图片分支，
    会落到 latin-1 兜底把二进制解成乱码段落并向量化——静默污染，必须挡住。"""
    paragraphs, assets = parser_extract.extract_text_with_assets(_PNG_1PX, "image/png")

    joined = "".join(p["text"] for p in paragraphs)
    assert "\x89" not in joined and "PNG" not in joined
    assert len(assets) == 1
    assert assets[0].kind == "image"
    assert assets[0].data == _PNG_1PX
    # 占位符必须真的出现在段落里，否则后面定位不到父块
    assert assets[0].placeholder in joined


def test_pdf_assets_carry_caption_and_locator(monkeypatch):
    _install_fake_parser(monkeypatch, _RICH_PAYLOAD)

    paragraphs, assets = parser_extract.extract_text_with_assets(b"%PDF-1.4", "application/pdf")

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

    monkeypatch.setattr(pdf_parser, "parse_pdf_rich", _boom)
    monkeypatch.setattr(file_parser, "parse_file", lambda *_a, **_k: "# 纯文本兜底")

    paragraphs, assets = parser_extract.extract_text_with_assets(b"%PDF-1.4", "application/pdf")

    assert assets == []
    assert "纯文本兜底" in "".join(p["text"] for p in paragraphs)


def test_with_assets_false_keeps_legacy_path(monkeypatch):
    """关掉多模态时不能再走重解析——parse_pdf_rich 一次都不该被调到。"""
    calls: list = []

    def _record(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("rich parse must not run when assets are disabled")

    monkeypatch.setattr(pdf_parser, "parse_pdf_rich", _record)
    monkeypatch.setattr(file_parser, "parse_file", lambda *_a, **_k: "# 正文")

    paragraphs, assets = parser_extract.extract_text_with_assets(
        b"%PDF-1.4", "application/pdf", with_assets=False
    )

    assert calls == []
    assert assets == []
    assert paragraphs


def test_asset_lands_on_the_chunk_that_contains_it(fake_storage, monkeypatch):
    """挂错父块的后果是检索命中后回溯到别处的正文——比丢图严重得多。"""
    monkeypatch.setattr(kb_assets_state, 'MIN_ASSET_BYTES', 0)
    parents = [
        _parent("p_first", "第一段没有图。"),
        _parent("p_second", "第二段：![](images/x.jpg) 见上图。"),
    ]
    assets = [
        parser_types.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
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
    monkeypatch.setattr(kb_assets_state, 'MIN_ASSET_BYTES', 0)
    parents = [_parent("p1", "见 ![](images/x.jpg) 图。")]
    assets = [
        parser_types.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
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
        parser_types.RawAsset(data=b"tiny", placeholder="images/tiny.jpg", mime_type="image/jpeg")
    ]

    created = kb_assets.persist_assets(
        _FakeSession(), kb_id="kb1", document_id="d1", user_id="u1", assets=assets, parents=parents
    )

    assert created == []
    assert fake_storage.objects == {}


def test_storage_failure_skips_one_asset_not_the_document(monkeypatch):
    monkeypatch.setattr(kb_assets_state, 'MIN_ASSET_BYTES', 0)

    class _BrokenStorage(_FakeStorage):
        def upload_bytes(self, content, storage_key):
            raise RuntimeError("bucket on fire")

    monkeypatch.setattr('core.files.storage.get_storage', lambda: _BrokenStorage())
    parents = [_parent("p1", "![](images/x.jpg)")]
    assets = [
        parser_types.RawAsset(data=b"x" * 100, placeholder="images/x.jpg", mime_type="image/jpeg")
    ]

    created = kb_assets.persist_assets(
        _FakeSession(), kb_id="kb1", document_id="d1", user_id="u1", assets=assets, parents=parents
    )

    assert created == []
    assert "images/x.jpg" in parents[0].content  # 没落盘就不该改写成死链


def test_no_vision_model_is_a_degradation_not_a_failure(monkeypatch):
    """未指派 vision 角色、主模型也不识图，是正常状态：图注照常入索引。
    这正是「视觉模型可以晚点再配」的前提——之后回填不用重新解析文档。"""
    monkeypatch.setattr(asset_captions, 'vision_available', lambda: False)
    rows = [_asset_row()]
    db = _FakeSession()

    done = asset_captions.generate_captions(db, rows)

    assert done == 0
    assert rows[0].caption_status == "skipped"
    assert "季度营收趋势" in asset_indexing.asset_index_text(rows[0], title="年报")


def test_evidence_lands_in_caption_and_ocr_merges_into_text(fake_storage, monkeypatch):
    """证据要落到正确的字段上：概述进 caption，OCR 并进 text_content（与图注共存）。
    并错了检索文本就废了——图注是作者写的用途，OCR 是图里实际的字，两者都要。"""
    fake_storage.objects["k1"] = b"x" * 100
    _install_bridge(monkeypatch, [_vision_result()])
    rows = [_asset_row()]

    done = asset_captions.generate_captions(_FakeSession(), rows)

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

    asset_captions.generate_captions(_FakeSession(), rows)

    assert rows[0].text_content == "图 1 季度营收趋势"


def test_text_content_is_capped(fake_storage, monkeypatch):
    """满是文字的扫描件能吐出几万字，直接入库会把行和向量文本一起撑爆。"""
    fake_storage.objects["k1"] = b"x" * 100
    monkeypatch.setattr(kb_assets_state, 'MAX_TEXT_CONTENT_CHARS', 50)
    _install_bridge(monkeypatch, [_vision_result(_evidence(ocr="字" * 500))])
    rows = [_asset_row()]

    asset_captions.generate_captions(_FakeSession(), rows)

    assert len(rows[0].text_content) == 50


def test_single_image_failure_keeps_asset_indexable(fake_storage, monkeypatch):
    """视觉桥对单张返回 None（任何失败都这样降级），资产仍要凭图注可检索。"""
    fake_storage.objects["k1"] = b"x" * 100
    _install_bridge(monkeypatch, [None])
    rows = [_asset_row()]

    done = asset_captions.generate_captions(_FakeSession(), rows)

    assert done == 0
    assert rows[0].caption_status == "failed"
    assert asset_indexing.asset_index_text(rows[0]).strip() != ""


def test_whole_batch_exception_does_not_fail_the_document(fake_storage, monkeypatch):
    fake_storage.objects["k1"] = b"x" * 100
    monkeypatch.setattr(asset_captions, 'vision_available', lambda: True)

    def _boom(_images, _loop=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(asset_captions, 'describe_batch', _boom)
    rows = [_asset_row()]

    assert asset_captions.generate_captions(_FakeSession(), rows) == 0
    assert rows[0].caption_status == "failed"


def test_kb_never_saturates_the_shared_vision_lane(fake_storage, monkeypatch):
    """视觉桥的并发信号量是进程级的：一篇图海文档一次性 gather 过去会把对话里的看图
    请求全排到后面。KB 必须按小批提交，任何时刻只占用其中几格。"""
    for i in range(7):
        fake_storage.objects[f"k{i}"] = b"x" * 100
    calls: list = []
    _install_bridge(monkeypatch, [], calls=calls)
    monkeypatch.setattr(kb_assets_state, 'CAPTION_BATCH', 2)
    rows = [_asset_row(asset_id=f"a{i}", storage_key=f"k{i}") for i in range(7)]

    asset_captions.generate_captions(_FakeSession(), rows)

    assert calls == [2, 2, 2, 1], f"批次切分不对: {calls}"
    assert max(calls) <= 2


def test_unreadable_asset_is_skipped_without_calling_vision(fake_storage, monkeypatch):
    """存储里读不到的那张不该占用一次视觉调用，也不该拖垮同批其他图。"""
    fake_storage.objects["k2"] = b"x" * 100
    calls: list = []
    _install_bridge(monkeypatch, [_vision_result()], calls=calls)
    monkeypatch.setattr(kb_assets_state, 'CAPTION_BATCH', 5)
    rows = [_asset_row(asset_id="a1", storage_key="missing"), _asset_row(asset_id="a2", storage_key="k2")]

    done = asset_captions.generate_captions(_FakeSession(), rows)

    assert calls == [1]  # 只送了能读出来的那张
    assert rows[0].caption_status == "failed"
    assert done == 1


def test_per_document_cap_marks_the_rest_explicitly(fake_storage, monkeypatch):
    """上限生效要在数据里看得见，否则和"视觉模型什么都没返回"分不开。"""
    for i in range(4):
        fake_storage.objects[f"k{i}"] = b"x" * 100
    _install_bridge(monkeypatch, [])
    monkeypatch.setattr(kb_assets_state, 'MAX_CAPTIONS_PER_DOC', 2)
    rows = [_asset_row(asset_id=f"a{i}", storage_key=f"k{i}") for i in range(4)]

    asset_captions.generate_captions(_FakeSession(), rows)

    assert [r.caption_status for r in rows[2:]] == ["skipped", "skipped"]


def test_index_text_prefers_caption_then_caption_text():
    row = _asset_row(caption="柱状图，Q4 最高，同比增长 32%")
    text = asset_indexing.asset_index_text(row, title="2026 年报")
    assert text.splitlines() == ["柱状图，Q4 最高，同比增长 32%", "图 1 季度营收趋势", "2026 年报"]


def test_asset_rows_point_at_their_parent_chunk(monkeypatch):
    """图片行必须挂在父块上：这样去重、回 PG 取父块正文、重排全都不用改。"""
    written: list[dict] = []
    monkeypatch.setattr(asset_indexing, 'asset_index_text', lambda a, title="": "柱状图描述")
    import features.knowledge.kb_vector as kb_vector

    monkeypatch.setattr(vector_embedding, 'embed_batch', lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr(vector_indexing, 'upsert_rows', lambda rows: written.extend(rows))

    count = asset_indexing.index_assets(
        [_asset_row()], user_id="u1", kb_id="kb1", document_id="d1", title="年报"
    )

    assert count == 1
    row = written[0]
    assert row["row_type"] == "image"
    assert row["parent_chunk_id"] == "p1"
    assert row["chunk_id"] != "p1"  # 主键不能和分块行撞

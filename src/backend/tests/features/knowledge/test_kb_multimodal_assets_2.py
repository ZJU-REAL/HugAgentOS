from tests.features.knowledge.kb_multimodal_assets_support import (
    _asset_row,
    _stub_bridge,
    asset_captions,
    asset_indexing,
    kb_assets,
    vector_embedding,
    vector_indexing,
)

def test_orphan_asset_falls_back_to_itself(monkeypatch):
    written: list[dict] = []
    import features.knowledge.kb_vector as kb_vector

    monkeypatch.setattr(vector_embedding, 'embed_batch', lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr(vector_indexing, 'upsert_rows', lambda rows: written.extend(rows))

    asset_indexing.index_assets(
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


def test_describe_batch_runs_on_the_host_loop_when_there_is_one(monkeypatch):
    """索引跑在 anyio 工作线程里时，协程必须交回宿主循环执行——只有走宿主循环，
    视觉桥的证据缓存与并发信号量才真的生效（也才不会各起各的循环）。"""
    import anyio
    import anyio.to_thread

    seen: dict = {}
    _stub_bridge(monkeypatch, seen)

    async def _main():
        return await anyio.to_thread.run_sync(
            lambda: asset_captions.describe_batch([(b"a", "image/png"), (b"b", "image/jpeg")])
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

    results = asset_captions.describe_batch([(b"a", "image/png")])

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
                r=asset_captions.describe_batch([(b"a", "image/png")], loop)
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


def test_media_only_parent_falls_back_to_the_asset_row_content():
    """单独上传的图片，其父块正文就是一条 markdown 图片链接。父块无条件覆盖的话，
    调用方拿到的只是个 URL，而命中的图片行里恰好装着描述与转写。"""
    from extensions.mcp_servers.retrieve_dataset_content_mcp.local_impl import _pick_content

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

from tests.features.knowledge.kb_wiki_end_to_end_support import (
    DOC_ID,
    INDEX_SLUG,
    KBChunk,
    KBWikiPage,
    KB_ID,
    LLMBudget,
    WikiConfig,
    _point_session_local,
    finalize_kb_wiki,
    local_graph,
    local_provider,
    pipeline,
    wiki_db,
)

def test_ingest_creates_pages_with_lineage(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    config = WikiConfig(map_parallel=1, reduce_parallel=1, citation_chunk_batch=8)

    result = pipeline.run_ingest(
        session, KB_ID, [DOC_ID], config=config, budget=LLMBudget(max_calls=100)
    )

    assert result.documents == 1
    assert result.pages_written == 2
    assert result.summaries_written == 1

    pages = {p.slug: p for p in session.query(KBWikiPage).all()}
    assert set(pages) == {"entity/acme", "concept/rag", f"summary/{DOC_ID}"}

    acme = pages["entity/acme"]
    # 血缘：引文标注给的短 ID 必须被映射回真实 chunk_id
    assert acme.chunk_refs == ["chunk-0", "chunk-2"]
    assert acme.source_refs == [DOC_ID]
    assert acme.title == "示例科技"
    assert "示例科技有限公司" in (acme.aliases or [])
    # 目录规划落成真实目录
    assert acme.category_path == ["组织"]
    assert acme.wiki_path == "组织"
    assert acme.folder_id

    rag = pages["concept/rag"]
    assert rag.chunk_refs == ["chunk-1"]
    assert rag.category_path == ["技术", "检索"]


def test_ingest_links_pages_to_each_other(wiki_db, stub_llm):
    """写页阶段漏链时，自动织链要把正文里的条目名补成 Wiki 链接。"""
    session, _ = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    acme = session.query(KBWikiPage).filter_by(slug="entity/acme").one()
    assert "[[concept/rag|检索增强生成]]" in acme.content
    assert "concept/rag" in (acme.out_links or [])


def test_finalize_builds_index_and_backlinks(wiki_db, stub_llm):
    session, _ = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    stats = finalize_kb_wiki(session, KB_ID, config=WikiConfig(), budget=LLMBudget(max_calls=10))
    session.commit()

    assert stats["pages"] == 3
    index = session.query(KBWikiPage).filter_by(slug=INDEX_SLUG).one()
    assert "实体" in index.content and "概念" in index.content

    # 反向链接由全库 out_links 反推，而不是增量维护
    rag = session.query(KBWikiPage).filter_by(slug="concept/rag").one()
    assert "entity/acme" in (rag.in_links or [])


def test_finalize_strips_dead_links(wiki_db, stub_llm):
    """指向已不存在页面的链接必须降级成纯文本，否则 Wiki 里会积累死链。"""
    session, _ = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    acme = session.query(KBWikiPage).filter_by(slug="entity/acme").one()
    acme.content = acme.content + "\n参见 [[entity/ghost|幽灵页]]。"
    acme.out_links = [*(acme.out_links or []), "entity/ghost"]
    session.commit()

    stats = finalize_kb_wiki(session, KB_ID, config=WikiConfig(), budget=LLMBudget(max_calls=10))
    session.commit()

    assert stats["dead_links_removed"] >= 1
    refreshed = session.query(KBWikiPage).filter_by(slug="entity/acme").one()
    assert "[[entity/ghost" not in refreshed.content
    assert "幽灵页" in refreshed.content
    assert "entity/ghost" not in (refreshed.out_links or [])


def test_retract_removes_document_lineage(wiki_db, stub_llm):
    """文档删除后，只由它供源的页面要归档，悬空的分块引用要摘掉。"""
    session, _ = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    session.query(KBChunk).filter_by(document_id=DOC_ID).delete()
    session.commit()

    stats = pipeline.run_retract(session, KB_ID, [DOC_ID])

    assert stats["archived"] == 3, "三个页面都只有这一个来源，应全部归档"
    live = session.query(KBWikiPage).filter(KBWikiPage.deleted_at.is_(None)).all()
    assert live == []


def test_read_surface_matches_expected_shape(wiki_db, stub_llm, monkeypatch):
    """读取面的字段是路由、MCP 工具与前端组件的共同契约，形状不能漂。"""
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    finalize_kb_wiki(session, KB_ID, config=WikiConfig(), budget=LLMBudget(max_calls=10))
    session.commit()
    _point_session_local(monkeypatch, Session)

    stats = local_provider.wiki_stats(KB_ID)
    assert stats["total_pages"] == 4  # 2 条目 + 1 摘要 + 1 索引
    assert stats["pages_by_type"]["entity"] == 1

    page = local_provider.wiki_read_page(KB_ID, "entity/acme")
    for key in (
        "slug",
        "title",
        "page_type",
        "type_label",
        "summary",
        "aliases",
        "category_path",
        "wiki_path",
        "out_links",
        "in_links",
        "source_refs",
        "chunk_refs",
        "content",
        "related_pages",
    ):
        assert key in page, f"读取面缺少字段 {key}"
    assert page["type_label"] == "实体"
    # 相关页要带上可读标题，否则模型和用户看到的都是一串拼音
    assert page["related_pages"][0]["title"] == "检索增强生成"


def test_source_chunks_resolve_by_id(wiki_db, stub_llm, monkeypatch):
    """顺血缘取原文是按 ID 直取，且要保持页面记录的引用顺序。"""
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    session.commit()
    _point_session_local(monkeypatch, Session)

    result = local_provider.wiki_source_chunks(KB_ID, "entity/acme", max_chunks=6)
    ids = [c["chunk_id"] for c in result["chunks"]]
    assert ids == ["chunk-0", "chunk-2"]
    assert result["chunks"][0]["document_title"] == "示例科技年度报告"
    assert "示例科技有限公司成立于" in result["chunks"][0]["content"]


def test_source_chunks_fall_back_when_refs_are_stale(wiki_db, stub_llm, monkeypatch):
    """页面被改写或文档重新索引后 chunk_refs 会对不上，此时退回该文档前几块。"""
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    page = session.query(KBWikiPage).filter_by(slug="entity/acme").one()
    page.chunk_refs = ["chunk-does-not-exist"]
    session.commit()
    _point_session_local(monkeypatch, Session)

    result = local_provider.wiki_source_chunks(KB_ID, "entity/acme", max_chunks=2)
    assert len(result["chunks"]) == 2, "对不上时不该空手而归"


def test_search_ranks_title_hits_above_body(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    session.commit()
    _point_session_local(monkeypatch, Session)

    result = local_provider.wiki_search(KB_ID, "检索增强生成", limit=5)
    assert result["pages"], "应命中概念页"
    assert result["pages"][0]["slug"] == "concept/rag"


def test_search_supports_alternation(wiki_db, stub_llm, monkeypatch):
    """口语说法常匹配不上书面术语，一次给多个说法要能命中。"""
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    session.commit()
    _point_session_local(monkeypatch, Session)

    slugs = {p["slug"] for p in local_provider.wiki_search(KB_ID, "示例科技|检索增强生成")["pages"]}
    assert {"entity/acme", "concept/rag"} <= slugs


def test_folders_report_recursive_page_counts(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    session.commit()
    _point_session_local(monkeypatch, Session)

    roots = local_provider.wiki_folders(KB_ID)
    by_name = {f["name"]: f for f in roots["folders"]}
    assert {"组织", "技术"} <= set(by_name)
    # 「技术」下的页面挂在子目录「检索」里，递归计数必须能看到它
    assert by_name["技术"]["page_count"] == 1
    assert by_name["技术"]["has_children"] is True
    assert by_name["组织"]["has_children"] is False


def test_graph_ego_mode_centres_on_node(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=100),
    )
    finalize_kb_wiki(session, KB_ID, config=WikiConfig(), budget=LLMBudget(max_calls=10))
    session.commit()
    _point_session_local(monkeypatch, Session)

    graph = local_graph.wiki_graph(KB_ID, mode="ego", center="entity/acme", depth=1)
    slugs = {n["slug"] for n in graph["nodes"]}
    assert "entity/acme" in slugs and "concept/rag" in slugs
    assert graph["edges"], "两页之间有链接，应产出边"


def test_graph_ego_on_missing_node_is_empty(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    _point_session_local(monkeypatch, Session)
    graph = local_graph.wiki_graph(KB_ID, mode="ego", center="entity/nope")
    assert graph["nodes"] == [] and graph["edges"] == []


def test_ingest_is_idempotent_on_rerun(wiki_db, stub_llm):
    """同一批文档重跑不该产生重复页——slug 复用规则要真的生效。"""
    session, _ = wiki_db
    config = WikiConfig(map_parallel=1, reduce_parallel=1)
    pipeline.run_ingest(session, KB_ID, [DOC_ID], config=config, budget=LLMBudget(max_calls=100))
    first = session.query(KBWikiPage).count()

    pipeline.run_ingest(session, KB_ID, [DOC_ID], config=config, budget=LLMBudget(max_calls=100))
    assert session.query(KBWikiPage).count() == first

    acme = session.query(KBWikiPage).filter_by(slug="entity/acme").one()
    assert acme.version >= 2, "重跑应写出新版本而不是新页面"


def test_concurrent_stages_never_touch_the_session(wiki_db, stub_llm):
    """并发段绝不能碰 session。

    SQLAlchemy 的 Session 不是线程安全的：从线程池里查库会炸成
    "concurrent operations are not permitted"，而且只在并发度 > 1 时才复现——
    单线程跑一万遍都发现不了。这里用一个会记录调用线程的代理把它钉死。
    """
    import threading

    session, _ = wiki_db
    main_thread = threading.get_ident()
    offenders: list[str] = []

    real_query = session.query

    def _tracking_query(*args, **kwargs):
        if threading.get_ident() != main_thread:
            offenders.append(threading.current_thread().name)
        return real_query(*args, **kwargs)

    session.query = _tracking_query  # type: ignore[method-assign]
    try:
        pipeline.run_ingest(
            session,
            KB_ID,
            [DOC_ID],
            config=WikiConfig(map_parallel=4, reduce_parallel=4),
            budget=LLMBudget(max_calls=100),
        )
    finally:
        session.query = real_query  # type: ignore[method-assign]

    assert offenders == [], f"这些线程在并发段里查了库：{offenders}"


def test_quota_stops_generation_without_failing(wiki_db, stub_llm):
    """超额是设计好的刹车：已生成的部分保留，作业不算失败。"""
    session, _ = wiki_db
    result = pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(map_parallel=1, reduce_parallel=1),
        budget=LLMBudget(max_calls=1),
    )
    assert result.quota_exceeded is True

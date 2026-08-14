"""Wiki 管线的端到端测试：喂真文档、桩掉模型、检查落库结果与读取面。

模型调用全部被桩掉——这里验的不是模型写得好不好，而是**管线接线对不对**：
候选是否落成页、引文是否变成可回溯的血缘、目录是否建起来、读取面返回的结构
是否与前端/工具期望的一致。这类接线错误在真实环境里要跑几十分钟才暴露一次。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import KBChunk, KBDocument, KBSpace, KBWikiPage, UserShadow
from core.kb.wiki import local_provider, pipeline
from core.kb.wiki.config import WikiConfig
from core.kb.wiki.finalize import INDEX_SLUG, finalize_kb_wiki
from core.kb.wiki.llm import LLMBudget

KB_ID = "kb_wikitest"
USER_ID = "u_wikitest"
DOC_ID = "doc_alpha"


@pytest.fixture
def wiki_db(tmp_path):
    """独立的 sqlite 库；local_provider 走自己的 SessionLocal，必须一并指向它。"""
    engine = create_engine(f"sqlite:///{tmp_path}/wiki.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    session = Session()
    session.add(UserShadow(user_id=USER_ID, username="wiki-tester"))
    session.add(
        KBSpace(
            kb_id=KB_ID,
            user_id=USER_ID,
            name="测试知识库",
            visibility="private",
            extra_data={"index_modes": ["rag", "wiki"]},
        )
    )
    session.add(
        KBDocument(
            document_id=DOC_ID,
            kb_id=KB_ID,
            title="示例科技年度报告",
            filename="report.md",
            size_bytes=100,
            mime_type="text/markdown",
            storage_key="k",
            indexing_status="completed",
        )
    )
    for index, text in enumerate(
        [
            "示例科技有限公司成立于 2020 年，主营企业级检索产品。",
            "检索增强生成（RAG）先召回文档再交给大模型生成答案。",
            "示例科技在 2024 年发布了基于 RAG 的问答平台。",
        ]
    ):
        session.add(
            KBChunk(
                chunk_id=f"chunk-{index}",
                kb_id=KB_ID,
                document_id=DOC_ID,
                chunk_index=index,
                content=text,
            )
        )
    session.commit()
    yield session, Session
    session.close()


@pytest.fixture
def stub_llm(monkeypatch):
    """按提示词特征分派桩响应，模拟各阶段的模型输出。"""
    calls: list[str] = []

    def _fake_call(*, system, user, budget=None, purpose="wiki", **kwargs):
        calls.append(purpose)
        if budget is not None:
            budget.check()
            budget.record(len(user), 100)

        if purpose == "候选抽取":
            return json.dumps(
                {
                    "entities": [
                        {
                            "name": "示例科技",
                            "slug": "entity/acme",
                            "aliases": ["示例科技有限公司"],
                            "description": "一家做企业级检索产品的公司。",
                            "details": "成立于 2020 年。",
                        }
                    ],
                    "concepts": [
                        {
                            "name": "检索增强生成",
                            "slug": "concept/rag",
                            "aliases": ["RAG"],
                            "description": "先检索再生成的技术路线。",
                            "details": "召回文档后交给大模型。",
                        }
                    ],
                }
            )
        if purpose == "引文标注":
            return json.dumps(
                {
                    "citations": {
                        "entity/acme": ["c001", "c003"],
                        "concept/rag": ["c002"],
                    },
                    "new_slugs": [],
                }
            )
        if purpose == "去重判定":
            return json.dumps({"merges": {}})
        if purpose == "目录规划":
            return json.dumps(
                {
                    "assignments": [
                        {"slug": "entity/acme", "path": ["组织"]},
                        {"slug": "concept/rag", "path": ["技术", "检索"]},
                    ]
                }
            )
        if purpose == "写页":
            return "SUMMARY: 该条目的一句话摘要。\n# 条目\n\n正文内容，提到 检索增强生成。"
        if purpose == "文档摘要":
            return "SUMMARY: 这篇文档讲示例科技。\n# 示例科技年度报告\n\n概要正文。"
        if purpose == "索引导语":
            return "# 测试知识库\n\n本库覆盖企业与检索技术。"
        return "{}"

    monkeypatch.setattr("core.kb.wiki.extract.call_llm_json", _wrap_json(_fake_call))
    monkeypatch.setattr("core.kb.wiki.cite.call_llm_json", _wrap_json(_fake_call))
    monkeypatch.setattr("core.kb.wiki.dedup.call_llm_json", _wrap_json(_fake_call))
    monkeypatch.setattr("core.kb.wiki.taxonomy.call_llm_json", _wrap_json(_fake_call))
    monkeypatch.setattr("core.kb.wiki.reduce.call_llm", _fake_call)
    monkeypatch.setattr("core.kb.wiki.finalize.call_llm", _fake_call)
    return calls


def _wrap_json(fake_call):
    from core.kb.wiki.llm import parse_json_object

    def _inner(*, system, user, budget=None, purpose="wiki", **kwargs):
        return parse_json_object(
            fake_call(system=system, user=user, budget=budget, purpose=purpose)
        )

    return _inner


def _point_session_local(monkeypatch, Session):
    """local_provider / store 用模块级 SessionLocal，测试里要一并指过去。"""
    monkeypatch.setattr("core.kb.wiki.local_provider.SessionLocal", Session)


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

    graph = local_provider.wiki_graph(KB_ID, mode="ego", center="entity/acme", depth=1)
    slugs = {n["slug"] for n in graph["nodes"]}
    assert "entity/acme" in slugs and "concept/rag" in slugs
    assert graph["edges"], "两页之间有链接，应产出边"


def test_graph_ego_on_missing_node_is_empty(wiki_db, stub_llm, monkeypatch):
    session, Session = wiki_db
    _point_session_local(monkeypatch, Session)
    graph = local_provider.wiki_graph(KB_ID, mode="ego", center="entity/nope")
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


def test_custom_instructions_reach_every_stage(wiki_db, monkeypatch):
    """用户填的要求必须真的送达每个阶段的模型调用。

    配置字段加了却没接到提示词上，是这类功能最典型的"看起来做了"——
    界面能填、能存库、生成结果毫无变化。这里逐阶段核实。
    """
    session, _ = wiki_db
    seen: dict[str, str] = {}

    def _capture(*, system, user, budget=None, purpose="wiki", **kwargs):
        seen[purpose] = f"{system or ''}\n{user}"
        if budget is not None:
            budget.record(len(user), 10)
        if purpose == "候选抽取":
            return json.dumps(
                {
                    "entities": [
                        {"name": "示例科技", "slug": "entity/acme", "aliases": [],
                         "description": "d", "details": "x"}
                    ],
                    "concepts": [],
                }
            )
        if purpose == "引文标注":
            return json.dumps({"citations": {"entity/acme": ["c001"]}, "new_slugs": []})
        if purpose in ("去重判定",):
            return json.dumps({"merges": {}})
        if purpose == "目录规划":
            return json.dumps({"assignments": []})
        if purpose in ("写页", "文档摘要"):
            return "SUMMARY: 摘要\n# 标题\n正文"
        return "{}"

    for module in ("extract", "cite", "dedup", "taxonomy"):
        monkeypatch.setattr(f"core.kb.wiki.{module}.call_llm_json", _wrap_json(_capture))
    monkeypatch.setattr("core.kb.wiki.reduce.call_llm", _capture)

    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(
            map_parallel=1,
            reduce_parallel=1,
            extraction_instructions="重点关注补助金额与申报条件",
            content_instructions="每页开头先给一句话结论",
        ),
        budget=LLMBudget(max_calls=100),
    )

    # 抽取侧两个阶段拿到抽取要求
    for stage in ("候选抽取", "引文标注"):
        assert "重点关注补助金额与申报条件" in seen[stage], f"{stage} 未收到抽取要求"
        assert "<extraction_business_instructions>" in seen[stage]

    # 撰写侧两个阶段拿到撰写要求
    for stage in ("写页", "文档摘要"):
        assert "每页开头先给一句话结论" in seen[stage], f"{stage} 未收到撰写要求"
        assert "<content_business_instructions>" in seen[stage]

    # 两侧不串味：抽取要求不该出现在写页提示词里
    assert "重点关注补助金额与申报条件" not in seen["写页"]


def test_worker_batches_sibling_ingest_jobs(wiki_db):
    """同库的多个待跑摄入作业要被合并成一批。

    上传是一篇文档一个作业（入队点在索引完成回调里，天然单篇）。若处理时也一篇
    一批，单 worker 串行跑 39 篇要数小时，而且 map_parallel 根本没有多篇文档可并行。
    """
    from core.kb.wiki import jobs as job_queue

    session, _ = wiki_db
    first = job_queue.enqueue_ingest(session, KB_ID, ["doc-a"])
    job_queue.enqueue_ingest(session, KB_ID, ["doc-b"])
    job_queue.enqueue_ingest(session, KB_ID, ["doc-c"])

    claimed = job_queue.claim_next_job(session, "worker-1")
    assert claimed is not None and claimed.job_id == first.job_id

    siblings = job_queue.claim_sibling_ingest_jobs(session, "worker-1", KB_ID, limit=4)
    assert [s.payload["document_ids"] for s in siblings] == [["doc-b"], ["doc-c"]]

    # 合批后全部一起结单，不留孤儿
    job_queue.finish_jobs(session, [claimed.job_id, *(s.job_id for s in siblings)])
    assert job_queue.kb_job_summary(session, KB_ID)["generating"] is False


def test_batching_never_crosses_knowledge_bases(wiki_db):
    """跨库合批会让一个大库把其他库饿死。"""
    from core.db.models import KBSpace
    from core.kb.wiki import jobs as job_queue

    session, _ = wiki_db
    session.add(
        KBSpace(kb_id="kb_other", user_id=USER_ID, name="别的库", visibility="private",
                extra_data={"index_modes": ["wiki"]})
    )
    session.commit()
    job_queue.enqueue_ingest(session, KB_ID, ["mine"])
    job_queue.enqueue_ingest(session, "kb_other", ["theirs"])

    siblings = job_queue.claim_sibling_ingest_jobs(session, "worker-1", KB_ID, limit=10)
    assert all(s.kb_id == KB_ID for s in siblings)

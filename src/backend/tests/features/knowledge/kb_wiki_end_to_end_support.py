"""Wiki 管线的端到端测试：喂真文档、桩掉模型、检查落库结果与读取面。

模型调用全部被桩掉——这里验的不是模型写得好不好，而是**管线接线对不对**：
候选是否落成页、引文是否变成可回溯的血缘、目录是否建起来、读取面返回的结构
是否与前端/工具期望的一致。这类接线错误在真实环境里要跑几十分钟才暴露一次。
"""


from __future__ import annotations


from features.knowledge.wiki import local_graph


from features.knowledge.wiki import local_provider


import json


import pytest


from sqlalchemy import create_engine


from sqlalchemy.orm import sessionmaker


from core.db.engine import Base


from features.knowledge.models import KBChunk, KBDocument, KBSpace, KBWikiPage


from core.db.models import UserShadow


from features.knowledge.wiki import local_provider, pipeline


from features.knowledge.wiki.config import WikiConfig


from features.knowledge.wiki.finalize import INDEX_SLUG, finalize_kb_wiki


from features.knowledge.wiki.llm import LLMBudget


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

    monkeypatch.setattr('features.knowledge.wiki.extract.call_llm_json', _wrap_json(_fake_call))
    monkeypatch.setattr('features.knowledge.wiki.cite.call_llm_json', _wrap_json(_fake_call))
    monkeypatch.setattr('features.knowledge.wiki.dedup.call_llm_json', _wrap_json(_fake_call))
    monkeypatch.setattr('features.knowledge.wiki.taxonomy.call_llm_json', _wrap_json(_fake_call))
    monkeypatch.setattr('features.knowledge.wiki.reduce.call_llm', _fake_call)
    monkeypatch.setattr('features.knowledge.wiki.finalize.call_llm', _fake_call)
    return calls


def _wrap_json(fake_call):
    from features.knowledge.wiki.llm import parse_json_object

    def _inner(*, system, user, budget=None, purpose="wiki", **kwargs):
        return parse_json_object(
            fake_call(system=system, user=user, budget=budget, purpose=purpose)
        )

    return _inner


def _point_session_local(monkeypatch, Session):
    """local_provider / store 用模块级 SessionLocal，测试里要一并指过去。"""
    monkeypatch.setattr('features.knowledge.wiki.local_provider.SessionLocal', Session)
    monkeypatch.setattr('features.knowledge.wiki.local_graph.SessionLocal', Session)

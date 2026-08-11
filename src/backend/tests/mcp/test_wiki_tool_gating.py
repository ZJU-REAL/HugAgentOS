"""知识库 MCP server：Wiki 工具按后端动态增减，且失败时降级为结构化错误。

LLM Wiki / 概念图谱是 WeKnora 独有的产物。选 dify / fastgpt 时这些工具**不应该
出现在工具清单里**——模型看不到就不会去调，省掉一轮注定失败的往返。
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import types

from mcp_servers.retrieve_dataset_content_mcp import server as srv
from mcp_servers.retrieve_dataset_content_mcp import wiki_impl

BASE_TOOLS = {"retrieve_dataset_content", "list_datasets", "retrieve_local_kb"}


async def _list_tool_names() -> set[str]:
    """走 lowlevel handler，覆盖到真实的 list_tools 链路而不是内部函数。"""
    handler = srv.mcp._mcp_server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(method="tools/list"))
    return {tool.name for tool in result.root.tools}


@pytest.fixture
def wiki_backend(monkeypatch):
    def _set(supported: bool):
        monkeypatch.setattr(wiki_impl, "wiki_supported", lambda: supported)

    return _set


@pytest.mark.parametrize("provider_supports_wiki", [False, True])
def test_tool_list_tracks_backend_capability(wiki_backend, provider_supports_wiki):
    wiki_backend(provider_supports_wiki)
    names = asyncio.run(_list_tool_names())

    assert BASE_TOOLS <= names, "基础检索工具在任何后端下都要在"
    if provider_supports_wiki:
        assert srv._WIKI_TOOL_NAMES <= names
    else:
        assert not (srv._WIKI_TOOL_NAMES & names), "非 Wiki 后端不得暴露 wiki 工具"


def test_capability_is_re_evaluated_per_call(wiki_backend):
    """管理台切换知识库后端后不应该还要重启 mcp 容器。"""
    wiki_backend(False)
    assert not (srv._WIKI_TOOL_NAMES & asyncio.run(_list_tool_names()))

    wiki_backend(True)
    assert srv._WIKI_TOOL_NAMES <= asyncio.run(_list_tool_names())

    wiki_backend(False)
    assert not (srv._WIKI_TOOL_NAMES & asyncio.run(_list_tool_names()))


def test_probe_failure_hides_wiki_tools(monkeypatch):
    """探测本身炸了要保守——宁可少暴露，也别给模型一个用不了的工具。"""

    def _boom():
        raise RuntimeError("probe down")

    monkeypatch.setattr(wiki_impl, "wiki_supported", _boom)
    assert not (srv._WIKI_TOOL_NAMES & asyncio.run(_list_tool_names()))


def test_unsupported_backend_returns_structured_error(monkeypatch):
    """工具真被调到时（比如模型记住了旧清单），要给可判读的错误而不是抛异常。"""
    monkeypatch.setattr(
        wiki_impl, "_wiki_module", lambda: (_ for _ in ()).throw(wiki_impl.WikiUnsupportedError("no wiki"))
    )
    result = asyncio.run(srv.wiki_locate("任意查询", dataset_id="kb-x"))

    assert result["pages"] == []
    assert result["error"]["code"] == "unsupported_backend"
    assert result["error"]["retryable"] is False


def test_locate_never_leaks_page_body(monkeypatch):
    """定位阶段只回摘要：正文动辄数千字，回全文就把「先定位再取回」的成本优势吃掉了。"""
    long_body = "正" * 5000

    class _Stub:
        @staticmethod
        def wiki_search(kb_id, query, limit):
            return {
                "pages": [
                    {
                        "slug": "concept/x",
                        "title": "某概念",
                        "page_type": "concept",
                        "type_label": "概念",
                        "summary": "摘" * 400,
                        "content": long_body,
                        "out_links": ["a", "b"],
                        "source_refs": ["d1"],
                    }
                ]
            }

    monkeypatch.setattr(wiki_impl, "_wiki_module", lambda: _Stub)
    monkeypatch.setattr(wiki_impl, "resolve_dataset_id", lambda *a, **k: "kb-x")

    result = wiki_impl.wiki_locate("查询", dataset_id="kb-x")
    entry = result["pages"][0]
    assert "content" not in entry
    assert len(entry["summary"]) <= wiki_impl._SUMMARY_MAX_CHARS + 1
    assert entry["related_count"] == 2
    assert entry["source_doc_count"] == 1


def test_fetch_source_shapes_items_for_citation(monkeypatch):
    """取回的原文要用「文件名称/文件内容」键，才能接上既有的引用卡片链路。"""

    class _Stub:
        @staticmethod
        def wiki_source_chunks(kb_id, slug, max_chunks):
            return {
                "page": {"title": "某概念"},
                "chunks": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_title": "运营办法.md",
                        "content": "第七条 …",
                    }
                ],
            }

    monkeypatch.setattr(wiki_impl, "_wiki_module", lambda: _Stub)
    monkeypatch.setattr(wiki_impl, "resolve_dataset_id", lambda *a, **k: "kb-x")

    result = wiki_impl.wiki_fetch_source("concept/x", dataset_id="kb-x")
    item = result["items"][0]
    assert item["文件名称"] == "运营办法.md"
    assert item["文件内容"] == "第七条 …"
    assert item["chunk_id"] == "c1"
    assert result["wiki_page"] == "某概念"


def test_resolve_dataset_id_prefers_request_allowlist(monkeypatch):
    monkeypatch.setattr(
        wiki_impl,
        "_wiki_module",
        lambda: pytest.fail("有白名单时不该再去列知识库"),
    )
    assert wiki_impl.resolve_dataset_id("", "kb-a, kb-b") == "kb-a"
    # 显式传入优先于白名单
    assert wiki_impl.resolve_dataset_id("kb-explicit", "kb-a") == "kb-explicit"

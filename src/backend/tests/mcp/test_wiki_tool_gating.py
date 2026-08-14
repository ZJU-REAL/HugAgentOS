"""知识库 MCP server：Wiki 工具的显隐、按库分发与失败降级。

Wiki 工具服务两类知识库——自建库（``kb_`` 前缀、勾了 Wiki 索引模式）与提供该
能力的外接后端。平台上一个 Wiki 源都没有时，这些工具**不应该出现在工具清单
里**：模型看不到就不会去调，省掉一轮注定失败的往返。

另有一条安全不变量：自建库必须过权限判定。Wiki 是原文的衍生物，可读性要与原文
一致，绝不能让这组工具成为绕过知识库授权的旁路。
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


@pytest.fixture
def stub_module(monkeypatch):
    """把 _wiki_module 换成给定 stub，并保持新签名（kb_id + 两个权限 kwarg）。"""

    def _install(stub):
        monkeypatch.setattr(wiki_impl, "_wiki_module", lambda kb_id, **kwargs: stub)

    return _install


@pytest.mark.parametrize("platform_has_wiki", [False, True])
def test_tool_list_tracks_platform_capability(wiki_backend, platform_has_wiki):
    wiki_backend(platform_has_wiki)
    names = asyncio.run(_list_tool_names())

    assert BASE_TOOLS <= names, "基础检索工具在任何配置下都要在"
    if platform_has_wiki:
        assert srv._WIKI_TOOL_NAMES <= names
    else:
        assert not (srv._WIKI_TOOL_NAMES & names), "没有任何 Wiki 源时不得暴露 wiki 工具"


def test_capability_is_re_evaluated_per_call(wiki_backend):
    """切换知识库后端、或新建一个开 Wiki 的自建库后，不应该还要重启 mcp 容器。"""
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


def test_unsupported_kb_returns_structured_error(monkeypatch):
    """工具真被调到时（比如模型记住了旧清单），要给可判读的错误而不是抛异常。"""

    def _raise(kb_id, **kwargs):
        raise wiki_impl.WikiUnsupportedError("no wiki")

    monkeypatch.setattr(wiki_impl, "_wiki_module", _raise)
    result = asyncio.run(srv.wiki_locate("任意查询", dataset_id="kb-x"))

    assert result["pages"] == []
    assert result["error"]["code"] == "unsupported_backend"
    assert result["error"]["retryable"] is False


def test_access_denied_surfaces_as_structured_error(monkeypatch):
    """无权访问的自建库要返回 access_denied，而不是把异常抛穿工具层。"""

    def _raise(kb_id, **kwargs):
        raise wiki_impl.WikiAccessDeniedError("无权访问")

    monkeypatch.setattr(wiki_impl, "_wiki_module", _raise)
    result = asyncio.run(srv.wiki_locate("任意查询", dataset_id="kb_private"))

    assert result["pages"] == []
    assert result["error"]["code"] == "access_denied"
    assert result["error"]["retryable"] is False


def test_local_kb_requires_permission(monkeypatch):
    """安全不变量：不在可见集里的自建库，取模块这一步就必须拦下。"""
    monkeypatch.setattr(wiki_impl, "_accessible_local_kb_ids", lambda _uid: ["kb_mine"])

    with pytest.raises(wiki_impl.WikiAccessDeniedError):
        wiki_impl._wiki_module("kb_someone_else", current_user_id="u1")


def test_external_kb_skips_local_permission_check(monkeypatch):
    """外接库的白名单在其实现模块内把关，不该被本地权限判定误伤。"""
    monkeypatch.setattr(wiki_impl, "_accessible_local_kb_ids", lambda _uid: [])
    sentinel = object()
    monkeypatch.setattr("core.kb.wiki_router.wiki_module_for", lambda kb_id: sentinel)

    assert wiki_impl._wiki_module("external-dataset", current_user_id="u1") is sentinel


def test_locate_never_leaks_page_body(monkeypatch, stub_module):
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

    stub_module(_Stub)
    monkeypatch.setattr(wiki_impl, "resolve_dataset_id", lambda *a, **k: "kb-x")

    result = wiki_impl.wiki_locate("查询", dataset_id="kb-x")
    entry = result["pages"][0]
    assert "content" not in entry
    assert len(entry["summary"]) <= wiki_impl._SUMMARY_MAX_CHARS + 1
    assert entry["related_count"] == 2
    assert entry["source_doc_count"] == 1


def test_fetch_source_shapes_items_for_citation(monkeypatch, stub_module):
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

    stub_module(_Stub)
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
        lambda *a, **k: pytest.fail("有白名单时不该再去列知识库"),
    )
    assert wiki_impl.resolve_dataset_id("", "kb-a, kb-b") == "kb-a"
    # 显式传入优先于白名单
    assert wiki_impl.resolve_dataset_id("kb-explicit", "kb-a") == "kb-explicit"


def test_resolve_dataset_id_prefers_wiki_capable_local_kb(monkeypatch):
    """自建库排在外接库前面：用户问的多半是自己传的资料。"""
    monkeypatch.setattr(
        "core.kb.wiki_router.wiki_capable_local_kb_ids",
        lambda ids: [i for i in ids if i == "kb_wiki_one"],
    )
    resolved = wiki_impl.resolve_dataset_id("", "external-a", allowed_kb_ids="kb_plain,kb_wiki_one")
    assert resolved == "kb_wiki_one"


def test_resolve_dataset_id_falls_back_to_external_when_no_local_wiki(monkeypatch):
    monkeypatch.setattr("core.kb.wiki_router.wiki_capable_local_kb_ids", lambda ids: [])
    resolved = wiki_impl.resolve_dataset_id("", "external-a", allowed_kb_ids="kb_plain")
    assert resolved == "external-a"

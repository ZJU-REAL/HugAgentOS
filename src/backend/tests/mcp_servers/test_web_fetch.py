"""Fetch response contract and explicit truncation."""

import asyncio

import httpx
from mcp_servers.web_fetch_mcp import impl


def test_fetch_keeps_url_title_and_marks_truncation(monkeypatch):
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(
            200, text="<html><title>Reference</title><body><p>abcdef</p></body></html>"
        )

    monkeypatch.setattr(
        impl.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = asyncio.run(impl.fetch_url("https://example.org/doc", max_chars=3))
    assert result["url"] == "https://example.org/doc"
    assert result["final_url"] == "https://example.org/doc"
    assert result["title"] == "Reference"
    assert result["content_truncated"] is True
    assert result["returned_chars"] == len(result["result"]) == 3


def test_download_budget_is_visible_and_source_is_citable(monkeypatch):
    import json

    from orchestration.citation_anchor import AnchorAllocator, annotate_tool_result

    real_client = httpx.AsyncClient
    monkeypatch.setattr(impl, "MAX_RESPONSE_BYTES", 16)
    monkeypatch.setattr(
        impl.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="a" * 40)), **kwargs
        ),
    )
    result = asyncio.run(impl.fetch_url("https://example.org", extract_mode="html"))
    assert result["download_truncated"] is True
    assert result["content_truncated"] is True
    assert result["result"] == "a" * 16
    _, citations = annotate_tool_result(
        "web_fetch", "fetch1", json.dumps(result), AnchorAllocator()
    )
    assert len(citations) == 1
    assert citations[0].url == "https://example.org"


def test_exact_content_limit_is_not_marked_truncated(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        impl.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="abcd")), **kwargs
        ),
    )
    result = asyncio.run(impl.fetch_url("https://example.org", extract_mode="html", max_chars=4))
    assert result["result"] == "abcd"
    assert result["content_truncated"] is False

"""Public retrieval selects one backend and preserves local access boundaries."""
from features.knowledge import kb_vector
from core.models import embedding as vector_embedding

import asyncio
import json
from types import SimpleNamespace

import pytest

from features.knowledge import edition as external_provider, kb_vector
from core.config.store.system_config import SystemConfigService
from extensions.mcp_servers.retrieve_dataset_content_mcp import impl, server, wiki_impl


def configure(monkeypatch, provider):
    monkeypatch.setattr(
        SystemConfigService,
        "get_instance",
        lambda: SimpleNamespace(
            get=lambda key: provider if key == "knowledge_base.provider" else None
        ),
    )


@pytest.fixture
def test_database_url(tmp_path):
    return f"sqlite:///{tmp_path / 'routing.db'}"


@pytest.fixture
def local_data(db_session, monkeypatch):
    from core.db import engine
    from features.knowledge.models import KBSpace
    from core.db.models import UserShadow
    from sqlalchemy.orm import sessionmaker

    db_session.add_all(
        [
            UserShadow(user_id="reader", username="reader"),
            UserShadow(user_id="other", username="other"),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            KBSpace(kb_id="kb_public", user_id="reader", name="Math", visibility="public"),
            KBSpace(kb_id="kb_private", user_id="reader", name="Private", visibility="private"),
            KBSpace(kb_id="kb_hidden", user_id="other", name="Hidden", visibility="public"),
            KBSpace(kb_id="kb_scoped", user_id="reader", name="Scoped", visibility="scoped"),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(vector_embedding, 'embed_text', lambda *args, **kwargs: [0.1])
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return [
            {
                "chunk_id": "multiplication",
                "kb_id": "kb_public",
                "content": "3 × 4 = 12",
                "title": "example.txt",
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(kb_vector, 'hybrid_search', search)
    return calls


def context(allowed=""):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                headers={
                    "x-current-user-id": "reader",
                    "x-allowed-kb-ids": allowed,
                }
            )
        )
    )


def test_local_public_call_only_searches_accessible_shared_spaces(monkeypatch, local_data):
    configure(monkeypatch, "custom")

    async def forbidden(**kwargs):
        pytest.fail("local mode must not call the external provider")

    monkeypatch.setattr(impl, "retrieve_dataset_content_async", forbidden)
    result = asyncio.run(server.retrieve_dataset_content(query="乘法定义", ctx=context()))

    assert result["items"][0]["content"] == "3 × 4 = 12"
    assert local_data[0]["public_kb_ids"] == ["kb_public", "kb_scoped"]
    assert local_data[0]["kb_ids"] == []


@pytest.mark.parametrize("dataset_id", ["kb_private", "kb_hidden", "external-id"])
def test_public_local_call_cannot_search_private_or_inaccessible_ids(
    monkeypatch, local_data, dataset_id
):
    configure(monkeypatch, "custom")
    result = asyncio.run(
        server.retrieve_dataset_content(
            query="乘法定义",
            dataset_id=dataset_id,
            ctx=context("kb_public,kb_private,kb_hidden"),
        )
    )
    assert result["items"] == []
    assert result["error"]["code"] == "access_denied"
    assert local_data == []


def test_empty_shared_selection_does_not_fall_back_to_all_local_kbs(monkeypatch, local_data):
    configure(monkeypatch, "custom")
    result = asyncio.run(
        server.retrieve_dataset_content(
            query="乘法定义",
            ctx=context("kb_private"),
        )
    )
    assert result["items"] == []
    assert local_data == []


@pytest.mark.parametrize("provider", ["dify", "fastgpt", "weknora"])
def test_external_selection_never_falls_back_to_local(monkeypatch, local_data, provider):
    configure(monkeypatch, provider)
    calls = []

    async def external(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(impl, "retrieve_dataset_content_async", external)
    result = asyncio.run(server.retrieve_dataset_content(query="policy", dataset_id="external-1"))
    assert result == {"items": []}
    assert calls[0]["dataset_id"] == "external-1"
    assert local_data == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["custom", "dify", "fastgpt", "weknora"])
async def test_public_tool_remains_available_for_selected_backend(monkeypatch, provider):
    from core.llm.mcp.lifetime import discover
    from tests.llm.test_mcp_parallel_execution import probe_server

    configure(monkeypatch, provider)
    monkeypatch.setattr(wiki_impl, "wiki_supported", lambda: False)
    async with probe_server(server.mcp) as (url, _):
        resource = await discover("retrieval", {"transport": "streamable_http", "url": url})
        try:
            names = {tool.name for tool in resource.tools}
            assert {"retrieve_dataset_content", "retrieve_local_kb", "list_datasets"} <= names
        finally:
            await resource.close()


@pytest.mark.asyncio
async def test_local_public_result_survives_mcp_protocol(monkeypatch, local_data):
    from core.llm.mcp.lifetime import discover
    from tests.llm.test_mcp_parallel_execution import probe_server

    configure(monkeypatch, "custom")
    async with probe_server(server.mcp) as (url, _):
        resource = await discover("retrieval", {"transport": "streamable_http", "url": url,
                                                "headers": {"X-Current-User-Id": "reader"}})
        try:
            tool = next(t for t in resource.tools if t.name == "retrieve_dataset_content")
            result = await tool(query="乘法定义")
            payload = result.metadata["mcp"]["structuredContent"]["result"]
            assert "items" in payload, payload
            assert payload["items"][0]["content"] == "3 × 4 = 12"
        finally:
            await resource.close()


@pytest.mark.parametrize("provider", ["custom", "fastgpt"])
def test_public_catalog_does_not_mix_storage_backends(monkeypatch, provider):
    configure(monkeypatch, provider)
    monkeypatch.setattr(
        impl,
        "_list_local_datasets",
        lambda **kwargs: (
            [{"kb_id": "kb_public"}],
            [{"kb_id": "kb_private"}],
        ),
    )

    def external(**kwargs):
        assert provider == "fastgpt", "local catalog must not contact external backend"
        return [{"dataset_id": "external-1"}]

    monkeypatch.setattr(impl, "list_external_datasets", external)
    result = impl.list_all_datasets(current_user_id="reader")
    assert result["public_datasets"] == (
        [{"kb_id": "kb_public"}] if provider == "custom" else [{"dataset_id": "external-1"}]
    )
    assert result["private_datasets"] == [{"kb_id": "kb_private"}]


def test_external_failure_does_not_search_local(monkeypatch, local_data):
    configure(monkeypatch, "fastgpt")

    async def fail(**kwargs):
        raise impl.DatasetRetrievalUnavailableError("unavailable")

    monkeypatch.setattr(impl, "retrieve_dataset_content_async", fail)
    result = asyncio.run(server.retrieve_dataset_content(query="policy"))
    assert result["error"]["code"] == "upstream_unavailable"
    assert local_data == []

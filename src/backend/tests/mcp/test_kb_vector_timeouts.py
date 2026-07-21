from __future__ import annotations

import sys
from types import ModuleType


def test_embed_text_uses_retrieval_timeout_and_caches_dimension(monkeypatch):
    from core.kb import kb_vector

    captured: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

    def _post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(
        kb_vector,
        "_resolve_embed_config",
        lambda: ("http://embedding.test/v1", "embed-model", "secret"),
    )
    monkeypatch.setattr(kb_vector.requests, "post", _post)
    kb_vector._DETECTED_DIM_CACHE.clear()

    vector = kb_vector.embed_text("query", timeout=4.5)

    assert vector == [0.1, 0.2, 0.3]
    assert captured["timeout"] == 4.5
    assert kb_vector._DETECTED_DIM_CACHE[("http://embedding.test/v1", "embed-model")] == 3


def test_hybrid_search_forwards_timeout_to_milvus(monkeypatch):
    from core.kb import kb_vector

    captured: dict = {}

    fake_pymilvus = ModuleType("pymilvus")
    fake_pymilvus.AnnSearchRequest = object
    fake_pymilvus.RRFRanker = object
    fake_pymilvus.MilvusClient = object
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)

    class _Client:
        def search(self, **kwargs):
            captured.update(kwargs)
            return [
                [
                    {
                        "distance": 0.9,
                        "entity": {
                            "chunk_id": "chunk-1",
                            "parent_chunk_id": "chunk-1",
                            "row_type": "chunk",
                            "kb_id": "kb-1",
                            "document_id": "doc-1",
                            "title": "title",
                            "content": "content",
                            "chunk_index": 0,
                        },
                    }
                ]
            ]

    monkeypatch.setattr(
        kb_vector, "get_or_create_collection", lambda **kwargs: captured.update(kwargs)
    )
    monkeypatch.setattr(kb_vector, "_get_client", lambda: _Client())
    monkeypatch.setattr(kb_vector, "_is_lite", lambda: True)

    hits = kb_vector.hybrid_search(
        "user-1",
        ["kb-1"],
        "query",
        [0.1, 0.2],
        timeout=6.0,
    )

    assert hits[0]["chunk_id"] == "chunk-1"
    assert captured["timeout"] == 6.0

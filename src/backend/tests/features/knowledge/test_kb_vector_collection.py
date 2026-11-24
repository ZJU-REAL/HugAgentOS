"""Collection lifecycle: resolution, caching, schema, search params, row shaping, write ordering."""
from __future__ import annotations

import pytest

from core.models import embedding as vector_embedding
from core.models import embedding_state
from features.knowledge import kb_vector, kb_vector_state, vector_client, vector_indexing
from tests.features.knowledge.kb_vector_support import CURRENT, LEGACY, FakeClient, install_fake_pymilvus


@pytest.fixture(autouse=True)
def server(monkeypatch):
    monkeypatch.setattr(vector_client, "is_lite", lambda: False)


@pytest.fixture
def current_client(monkeypatch):
    """A resolved current collection; collection creation is out of the picture."""
    client = FakeClient({CURRENT})
    monkeypatch.setattr(vector_client, "get_client", lambda: client)
    monkeypatch.setattr(vector_client, "get_or_create_collection", lambda **_: None)
    return client


@pytest.mark.parametrize(
    "existing, expected, index",
    [
        ({LEGACY}, LEGACY, "ivf"),
        ({LEGACY, CURRENT}, CURRENT, "hnsw"),
        (set(), CURRENT, "hnsw"),
    ],
)
def test_server_resolution_keeps_legacy_until_migrated(existing, expected, index):
    assert vector_client.resolve_collection(FakeClient(existing)) == expected
    assert kb_vector_state.COLLECTION_NAME == expected
    assert kb_vector_state.ACTIVE_INDEX == index


def test_lite_keeps_legacy_collection(monkeypatch):
    monkeypatch.setattr(vector_client, "is_lite", lambda: True)

    assert vector_client.resolve_collection(FakeClient({CURRENT})) == LEGACY
    assert kb_vector_state.ACTIVE_INDEX == "lite"


def test_ready_collection_costs_no_round_trip(monkeypatch):
    client = FakeClient({CURRENT}, dim=8)
    monkeypatch.setattr(vector_client, "get_client", lambda: client)
    monkeypatch.setattr(vector_embedding, "detect_embed_dim", lambda: 8)

    vector_client.get_or_create_collection()
    first = len(client.calls)
    for _ in range(5):
        vector_client.get_or_create_collection()

    assert first > 0
    assert len(client.calls) == first


def test_existing_collection_is_remembered_for_deletes():
    client = FakeClient({CURRENT})

    for _ in range(3):
        assert vector_client.existing_collection(client)

    assert client.made("has_collection") == [CURRENT, CURRENT]  # resolve + existence, once


def test_failed_write_forgets_cached_collection(monkeypatch):
    class Broken(FakeClient):
        def insert(self, **kwargs):
            raise RuntimeError("collection not found")

    monkeypatch.setattr(kb_vector_state, "ACTIVE_INDEX", "hnsw")
    monkeypatch.setattr(kb_vector_state, "COLLECTION_EXISTS", True)
    monkeypatch.setattr(kb_vector_state, "VERIFIED_COLLECTION_DIM", 8)

    with pytest.raises(RuntimeError):
        vector_client.insert(Broken(), [{"chunk_id": "x", "sparse_text": ""}])

    assert kb_vector_state.ACTIVE_INDEX is None
    assert kb_vector_state.COLLECTION_EXISTS is False
    assert kb_vector_state.VERIFIED_COLLECTION_DIM is None


def test_new_server_collection_uses_hnsw_partition_key_and_bm25(monkeypatch):
    install_fake_pymilvus(monkeypatch)
    client = FakeClient()
    monkeypatch.setattr(vector_client, "get_client", lambda: client)
    monkeypatch.setattr(vector_embedding, "detect_embed_dim", lambda: 8)
    monkeypatch.setattr(vector_embedding, "measured_embed_dim", lambda: 8)

    vector_client.get_or_create_collection()

    schema, indexes, kwargs = client.created[CURRENT]
    assert schema.fields["kb_id"].get("is_partition_key") is True
    assert schema.fields["dense_embedding"]["dim"] == 8
    assert indexes.indexes["dense_embedding"]["index_type"] == "HNSW"
    assert indexes.indexes["parent_chunk_id"]["index_type"] == "INVERTED"
    assert kwargs["num_partitions"] == kb_vector_state.PARTITION_COUNT
    assert schema.fields["sparse_text"]["enable_analyzer"] is True
    assert schema.fields["sparse_text"]["analyzer_params"]["tokenizer"] == "jieba"
    assert schema.functions == [{
        "name": "sparse_text_bm25", "function_type": "BM25",
        "input_field_names": ["sparse_text"], "output_field_names": ["sparse_embedding"],
    }]
    assert indexes.indexes["sparse_embedding"]["metric_type"] == "BM25"
    assert kb_vector_state.VERIFIED_COLLECTION_DIM == 8


def test_collection_is_not_created_at_a_guessed_dimension(monkeypatch):
    install_fake_pymilvus(monkeypatch)
    client = FakeClient()
    monkeypatch.setattr(vector_client, "get_client", lambda: client)
    monkeypatch.setattr(vector_embedding, "detect_embed_dim", lambda: 1024)
    monkeypatch.setattr(vector_embedding, "measured_embed_dim", lambda: None)

    with pytest.raises(RuntimeError, match="无法确认向量维度"):
        vector_client.get_or_create_collection()

    assert client.created == {}


@pytest.mark.parametrize(
    "index, params",
    [
        ("hnsw", {"metric_type": "IP", "params": {"ef": 90}}),
        ("ivf", {"metric_type": "IP", "params": {"nprobe": 32}}),
        ("lite", {"metric_type": "IP"}),
    ],
)
def test_dense_search_params_follow_the_active_index(monkeypatch, index, params):
    monkeypatch.setattr(kb_vector_state, "ACTIVE_INDEX", index)

    assert vector_client.dense_search_params(90) == params


def test_failed_dimension_probe_is_not_retried_immediately(monkeypatch):
    calls = []

    def _down(*_args, **_kwargs):
        calls.append(1)
        raise ConnectionError("embedding down")

    monkeypatch.setattr(vector_embedding, "resolve_embed_config", lambda: ("http://e", "m", ""))
    monkeypatch.setattr(vector_embedding, "embed_text", _down)
    state = embedding_state.process_state()
    monkeypatch.setattr(state, "DETECTED_DIM_CACHE", {})
    monkeypatch.setattr(state, "FAILED_DIM_PROBE_AT", {})

    for _ in range(3):
        assert vector_embedding.detect_embed_dim() == embedding_state.EMBED_DIMS

    assert len(calls) == 1


def _embedding_down(*_args, **_kwargs):
    raise ConnectionError("embedding down")


def test_question_edit_keeps_old_rows_when_embedding_fails(monkeypatch, current_client):
    monkeypatch.setattr(vector_embedding, "embed_batch", _embedding_down)

    with pytest.raises(ConnectionError):
        vector_indexing.upsert_question_rows("p1", ["q?"], "u", "kb", "doc", "t", 0)

    assert current_client.made("delete") == []


def test_chunk_edit_keeps_old_rows_when_embedding_fails(monkeypatch, current_client):
    monkeypatch.setattr(vector_embedding, "embed_text", _embedding_down)

    with pytest.raises(ConnectionError):
        vector_indexing.reindex_chunk_content("p1", "new", [], "u", "kb", "doc", "t", 0)

    assert current_client.made("delete") == []


def test_new_rows_are_inserted_in_batches(current_client):
    vector_indexing.insert_rows([{"chunk_id": str(i), "sparse_text": ""} for i in range(250)])

    assert [len(c["data"]) for c in current_client.made("insert")] == [100, 100, 50]
    assert current_client.made("upsert") == []


@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda: vector_indexing.delete_space_vectors('kb_"x'), 'kb_id == "kb_\\"x"'),
        (lambda: vector_indexing.delete_document_vectors("doc_abc"), 'document_id == "doc_abc"'),
        (
            lambda: vector_indexing.delete_document_vectors("doc_abc", "kb_1"),
            'kb_id == "kb_1" and document_id == "doc_abc"',
        ),
        (
            lambda: vector_indexing.delete_by_chunk("p1", "kb_1"),
            'kb_id == "kb_1" and parent_chunk_id == "p1"',
        ),
    ],
)
def test_deletes_are_not_owner_scoped_and_prune_by_space(current_client, call, expected):
    call()

    assert current_client.made("delete") == [{"collection_name": CURRENT, "filter": expected}]


def test_delete_skips_missing_collection(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(vector_client, "get_client", lambda: client)
    monkeypatch.setattr(
        vector_client, "get_or_create_collection",
        lambda **_: pytest.fail("deleting must not create the collection"),
    )

    vector_indexing.delete_document_vectors("doc_abc")

    assert client.made("delete") == []


def test_build_question_rows_keeps_each_vector_with_its_question():
    rows = vector_indexing.build_question_rows(
        "p1", ["a?", "b?"], [[1.0], [2.0]],
        user_id="u", kb_id="kb", document_id="doc", title="t", chunk_index=3,
    )

    assert [(r["chunk_id"], r["content"], r["dense_embedding"]) for r in rows] == [
        ("q_p1_0", "a?", [1.0]),
        ("q_p1_1", "b?", [2.0]),
    ]
    assert {r["row_type"] for r in rows} == {"question"}


ROW = {"chunk_id": "c", "content": "截断后的正文", "sparse_text": "事业单位的编制核定 [编办]"}


@pytest.mark.parametrize(
    "index, expected",
    [
        ("hnsw", {"chunk_id": "c", "content": ROW["content"], "sparse_text": ROW["sparse_text"]}),
        ("ivf", {"chunk_id": "c", "content": ROW["content"],
                 "sparse_embedding": kb_vector.text_to_sparse(ROW["sparse_text"])}),
        ("lite", {"chunk_id": "c", "content": ROW["content"]}),
    ],
)
def test_rows_are_shaped_for_each_collection_kind(index, expected):
    original = dict(ROW, sparse_embedding={1: 1.0})

    assert vector_client.adapt_rows([original], index=index) == [expected]
    assert original == dict(ROW, sparse_embedding={1: 1.0})  # caller's row untouched


def test_sparse_text_is_cut_to_the_field_limit():
    (row,) = vector_client.adapt_rows([{"chunk_id": "c", "sparse_text": "编" * 20000}], index="hnsw")

    assert len(row["sparse_text"].encode()) <= kb_vector_state.SPARSE_TEXT_FIELD_MAX_BYTES


@pytest.mark.parametrize(
    "index, expected",
    [
        ("hnsw", (["编制核定"], {"metric_type": "BM25"})),
        ("ivf", ([kb_vector.text_to_sparse("编制核定")], {"metric_type": "IP"})),
    ],
)
def test_sparse_query_matches_how_the_collection_was_indexed(monkeypatch, index, expected):
    monkeypatch.setattr(kb_vector_state, "ACTIVE_INDEX", index)

    assert vector_client.sparse_search_request("编制核定") == expected

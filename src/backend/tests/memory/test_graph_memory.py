"""L3 graph memory keeps declarative relations separate and actionable."""

from types import SimpleNamespace

import pytest
from core.memory import graph as G
from core.memory import service as S
from core.memory.context import MemoryContext
from core.memory.extractors import writers as W
from core.memory.extractors.router import ExtractorType
from core.memory.retrieval_types import RetrievedRelation


def test_relation_parser_accepts_only_the_bounded_graph_vocabulary():
    relation = G.parse_relation(
        {
            "source": "项目北斗",
            "source_type": "project",
            "predicate": "depends_on",
            "target": "统一身份认证平台",
            "target_type": "system",
            "confidence": 1.7,
        }
    )
    assert relation is not None
    assert relation.relationship == "依赖"
    assert relation.confidence == 1.0
    assert G.parse_relation({"source": "A", "predicate": "invented", "target": "B"}) is None
    assert (
        G.parse_relation(
            {
                "source": "项目北斗",
                "predicate": "depends_on",
                "target": "统一身份认证平台",
                "confidence": 0.4,
            }
        )
        is None
    )


def test_relation_parser_deduplicates_one_turn():
    raw = {
        "source": "项目北斗",
        "source_type": "project",
        "predicate": "uses",
        "target": "数据中台",
        "target_type": "system",
    }
    assert len(G.parse_relations([raw, dict(raw)])) == 1


def test_graph_read_uses_a_non_reserved_cypher_parameter(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, statement, **parameters):
            assert "$search_text" in statement
            assert parameters["search_text"] == "Project Polaris"
            return []

    class Driver:
        @staticmethod
        def session():
            return Session()

    monkeypatch.setattr(G, "_get_driver", lambda: Driver())
    assert (
        G._read_sync(
            scope_user_id="u1",
            workspace_id="default",
            limit=5,
            query="Project Polaris",
        )
        == []
    )


def test_graph_outbox_effect_receipt_does_not_reinforce_on_replay(monkeypatch):
    state = {"seen_count": 0, "effects": set()}

    class Result:
        def __init__(self, record):
            self._record = record

        def single(self):
            return self._record

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, statement, **parameters):
            assert "$effect_id IN coalesce(r.outbox_effect_ids, [])" in statement
            effect_id = parameters["effect_id"]
            replayed = effect_id in state["effects"]
            if not replayed:
                state["effects"].add(effect_id)
                state["seen_count"] += 1
            return Result(
                {
                    "relation_id": parameters["relation_id"],
                    "seen_count": state["seen_count"],
                    "replayed": replayed,
                }
            )

    class Driver:
        @staticmethod
        def session():
            return Session()

    monkeypatch.setattr(G, "_get_driver", lambda: Driver())
    relation = G.parse_relation(
        {
            "source": "项目北斗",
            "source_type": "project",
            "predicate": "depends_on",
            "target": "统一身份认证平台",
            "target_type": "system",
        }
    )
    ctx = MemoryContext(user_id="u1", effect_id="mout-candidate-1")

    first = G._write_sync(ctx, [relation])
    second = G._write_sync(ctx, [relation])

    assert first[0]["seen_count"] == 1
    assert first[0]["replayed"] is False
    assert second[0]["seen_count"] == 1
    assert second[0]["replayed"] is True


@pytest.mark.asyncio
async def test_graph_writer_reports_an_addressable_l3_relation(monkeypatch):
    monkeypatch.setattr(
        W,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(graph_enabled=True)),
    )

    async def fake_write(_ctx, relations):
        assert relations[0].predicate == "belongs_to"
        return [
            {
                **relations[0].to_dict(),
                "relation_id": "grel-42",
                "seen_count": 1,
            }
        ]

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(W, "write_graph_relations", fake_write)
    monkeypatch.setattr(W, "audit_record_batch", fake_audit)
    ctx = MemoryContext(user_id="u1", write_enabled=True, message_id="m1")

    written = await W.write_layered(
        {
            ExtractorType.GRAPH: {
                "relations": [
                    {
                        "source": "研发一组",
                        "source_type": "team",
                        "predicate": "belongs_to",
                        "target": "研发中心",
                        "target_type": "organization",
                    }
                ]
            }
        },
        ctx,
    )

    assert written == [
        {
            "layer": "L3",
            "kind": "graph_relation",
            "handle": "grel-42",
            "text": "研发一组 → 隶属于 → 研发中心",
            "action": "write",
        }
    ]


@pytest.mark.asyncio
async def test_retrieved_relations_carry_identity_from_the_graph_store(monkeypatch):
    """Retrieval must not discard the id/confidence the store already returns."""
    monkeypatch.setattr(
        S,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(enabled=True, graph_enabled=True)),
    )

    async def fake_search(_scope_user_id, _query, *, workspace_id, limit):
        return [
            {
                "source": "项目北斗",
                "relationship": "依赖",
                "target": "统一身份认证平台",
                "relation_id": "grel_x1",
                "predicate": "depends_on",
                "confidence": 0.87,
            }
        ]

    monkeypatch.setattr("core.memory.graph.search_graph_relations", fake_search)
    relations = await S._retrieve_graph_relations(
        user_id="u1", workspace_id="default", query="项目北斗", limit=5
    )
    relation = relations[0]
    assert relation.relation_id == "grel_x1"
    assert relation.predicate == "depends_on"
    assert relation.confidence == pytest.approx(0.87)


@pytest.mark.asyncio
async def test_graph_relations_survive_a_vector_store_outage(monkeypatch):
    monkeypatch.setattr(
        S,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(enabled=True, graph_enabled=True)),
    )
    monkeypatch.setattr(S, "_get_memory", lambda: None)

    async def fake_graph(**_kwargs):
        return (
            RetrievedRelation(
                source="项目北斗",
                relationship="依赖",
                target="统一身份认证平台",
                rank=1,
            ),
        )

    monkeypatch.setattr(S, "_retrieve_graph_relations", fake_graph)
    result = await S.retrieve_memories_structured("u1", "项目北斗依赖什么？")
    assert result.degraded is True
    assert result.degrade_reason == "store_unavailable"
    assert result.relations[0].target == "统一身份认证平台"

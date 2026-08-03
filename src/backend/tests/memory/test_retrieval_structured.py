"""GCE ticket 01 — structured memory retrieval + ref shadow.

The load-bearing test here is text equivalence: ``retrieve_memories()`` is on
the user's critical path and its output is injected verbatim into the prompt, so
turning it into a projection of a structured result must not shift a single
byte.  Everything else in the ticket is additive.
"""

import pytest

from core.memory.retrieval_types import (
    LAYER_FACT,
    LAYER_GRAPH,
    MemoryRetrievalResult,
    RetrievedMemory,
    RetrievedRelation,
    build_memory_item,
    content_hash,
)


def _legacy_render(filtered_items, relations):
    """The pre-ticket-01 formatting, copied verbatim as the oracle."""
    if not filtered_items and not relations:
        return ""
    lines = ["## 关于该用户的已知背景信息（来自历史会话记忆）"]
    for m in filtered_items:
        text = (m.get("memory") or "").strip()
        if text:
            lines.append(f"- {text}")
    if relations:
        lines.append("\n## 用户相关实体关系")
        for r in relations[:5]:
            if not isinstance(r, dict):
                continue
            src = r.get("source", "")
            rel = r.get("relationship", "")
            tgt = r.get("target", "")
            if src and rel and tgt:
                lines.append(f"- {src} → {rel} → {tgt}")
    return "\n".join(lines)


def _to_result(raw_items, raw_relations):
    items = []
    for index, raw in enumerate(raw_items, start=1):
        typed = build_memory_item(raw, rank=index)
        if typed is not None:
            items.append(typed)
    relations = []
    for index, r in enumerate(raw_relations[:5], start=1):
        if not isinstance(r, dict):
            continue
        relations.append(
            RetrievedRelation(
                source=r.get("source", ""),
                relationship=r.get("relationship", ""),
                target=r.get("target", ""),
                rank=index,
            )
        )
    return MemoryRetrievalResult(items=tuple(items), relations=tuple(relations))


# ── Text equivalence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_items,raw_relations",
    [
        ([], []),
        ([{"id": "m1", "memory": "用户偏好 Excel", "score": 0.9}], []),
        (
            [
                {"id": "m1", "memory": "用户偏好 Excel", "score": 0.9},
                {"id": "m2", "memory": "常驻杭州", "score": 0.7},
            ],
            [],
        ),
        # Blank content is skipped in the body but still counts as "not empty",
        # so the header survives alone — an easy edge case to regress on.
        ([{"id": "m1", "memory": "   ", "score": 0.9}], []),
        (
            [{"id": "m1", "memory": "用户偏好 Excel", "score": 0.9}],
            [{"source": "张三", "relationship": "任职于", "target": "某公司"}],
        ),
        # Incomplete triples are dropped.
        ([], [{"source": "张三", "relationship": "", "target": "某公司"}]),
        # Only the first five relations render.
        (
            [],
            [
                {"source": f"s{i}", "relationship": "rel", "target": f"t{i}"}
                for i in range(8)
            ],
        ),
    ],
)
def test_to_text_matches_legacy_rendering(raw_items, raw_relations):
    assert _to_result(raw_items, raw_relations).to_text() == _legacy_render(
        raw_items, raw_relations
    )


def test_degraded_result_renders_empty_text():
    for reason in ("disabled", "breaker_open", "timeout", "search_failed"):
        result = MemoryRetrievalResult.degraded_result(reason)
        assert result.to_text() == ""
        assert result.degraded is True
        assert result.degrade_reason == reason


# ── Structured identity ──────────────────────────────────────────────────────


def test_items_carry_id_layer_score_and_rank():
    result = _to_result(
        [
            {"id": "m1", "memory": "偏好 Excel", "score": 0.91, "_adjusted_score": 0.95},
            {"id": "m2", "memory": "常驻杭州", "score": 0.72},
        ],
        [],
    )
    first, second = result.items
    assert (first.memory_id, first.rank, first.layer) == ("m1", 1, LAYER_FACT)
    assert first.score == pytest.approx(0.91)
    assert first.adjusted_score == pytest.approx(0.95)
    # Missing adjusted score falls back to the raw score rather than zero.
    assert second.adjusted_score == pytest.approx(0.72)
    assert second.rank == 2


def test_metadata_scope_and_confidentiality_preserved():
    result = _to_result(
        [
            {
                "id": "m1",
                "memory": "内部资料",
                "score": 0.8,
                "metadata": {"workspace_id": "ws-9", "confidentiality": "internal"},
            }
        ],
        [],
    )
    item = result.items[0]
    assert item.workspace_id == "ws-9"
    assert item.confidentiality == "internal"


def test_malformed_records_are_dropped_not_raised():
    assert build_memory_item("not-a-dict", rank=1) is None
    assert build_memory_item(None, rank=1) is None
    # A non-numeric score must not blow up retrieval.
    item = build_memory_item({"id": "m1", "memory": "x", "score": "oops"}, rank=1)
    assert item is not None and item.score == 0.0


def test_event_payload_never_carries_raw_content():
    result = _to_result([{"id": "m1", "memory": "非常敏感的原文", "score": 0.8}], [])
    payload = result.to_event_payload()
    serialized = repr(payload)
    assert "非常敏感的原文" not in serialized
    assert payload["items"][0]["memory_id"] == "m1"
    assert payload["injected"] == 1


def test_graph_relations_reported_as_graph_layer():
    result = _to_result(
        [], [{"source": "A", "relationship": "属于", "target": "B"}]
    )
    assert result.relations[0].to_event_payload()["layer"] == LAYER_GRAPH


# ── Content hash / ref stability ─────────────────────────────────────────────


def test_content_hash_ignores_whitespace_reformatting():
    assert content_hash("用户  偏好\n Excel") == content_hash("用户 偏好 Excel")
    assert content_hash("a") != content_hash("b")


def test_ref_id_is_stable_across_external_id_churn():
    from core.memory.ref_shadow import build_ref_id

    # Same content, same user, same workspace → same ref, regardless of what the
    # external store calls it today. This is the whole point of the shadow map.
    args = dict(layer=LAYER_FACT, user_id="u1", workspace_id="default")
    ref_a = build_ref_id(content_hash=content_hash("偏好 Excel"), **args)
    ref_b = build_ref_id(content_hash=content_hash("偏好  Excel "), **args)
    assert ref_a == ref_b

    # Different user or workspace must not collide — refs are scoped.
    other_user = build_ref_id(
        layer=LAYER_FACT,
        user_id="u2",
        workspace_id="default",
        content_hash=content_hash("偏好 Excel"),
    )
    other_ws = build_ref_id(
        layer=LAYER_FACT,
        user_id="u1",
        workspace_id="ws-2",
        content_hash=content_hash("偏好 Excel"),
    )
    assert len({ref_a, other_user, other_ws}) == 3


def test_refs_for_items_matches_build_ref_id():
    from core.memory.ref_shadow import build_ref_id, refs_for_items

    item = RetrievedMemory(
        memory_id="m1",
        layer=LAYER_FACT,
        content="偏好 Excel",
        content_hash=content_hash("偏好 Excel"),
        score=0.9,
        adjusted_score=0.9,
        rank=1,
    )
    assert refs_for_items([item], user_id="u1") == [
        build_ref_id(
            layer=LAYER_FACT,
            user_id="u1",
            workspace_id="default",
            content_hash=item.content_hash,
        )
    ]

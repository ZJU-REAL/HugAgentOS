"""Tests for ``routing.citations.extract_citations_with_offset``.

The helper de-collides citation ids when the same tool is called more than
once within a single turn / batch item. ``extract_citations`` numbers ids
``<tool_name>-<index>`` from 1 *per call*, so without the offset rewrite a
ReAct loop that searches twice would emit duplicate ids — breaking frontend
reference chips and any id-keyed dedup downstream (trajectory distillation,
report export).

The helper is shared by all three call sites: ``routing/workflow.py`` (the two
main-chat streaming branches) and ``routing/batch_orchestrator.py``.

``routing.citations`` pulls in no heavy deps (json / dataclasses / typing), so
this imports the real function — no AST/stub gymnastics needed.
"""

from __future__ import annotations

from orchestration.citations import extract_citations_with_offset


def _internet_result(n: int) -> dict:
    """A fake internet_search result carrying *n* hits → n citations."""
    return {"result": [{"title": f"t{i}", "url": f"u{i}", "content": "x"} for i in range(n)]}


def test_first_call_unchanged_when_offset_zero() -> None:
    offsets: dict = {}
    items = extract_citations_with_offset("internet_search", "tc1", _internet_result(2), offsets)
    assert [c.id for c in items] == ["internet_search-1", "internet_search-2"]
    assert offsets["internet_search"] == 2


def test_same_tool_two_calls_no_collision() -> None:
    offsets: dict = {}
    first = extract_citations_with_offset("internet_search", "tc1", _internet_result(2), offsets)
    second = extract_citations_with_offset("internet_search", "tc2", _internet_result(2), offsets)
    ids = [c.id for c in first + second]
    assert ids == [
        "internet_search-1",
        "internet_search-2",
        "internet_search-3",
        "internet_search-4",
    ]
    assert len(ids) == len(set(ids))
    assert offsets["internet_search"] == 4


def test_three_call_chain_produces_unique_ids() -> None:
    offsets: dict = {}
    all_ids: list[str] = []
    for tc in ("tc1", "tc2", "tc3"):
        all_ids += [
            c.id
            for c in extract_citations_with_offset(
                "internet_search", tc, _internet_result(2), offsets
            )
        ]
    assert len(all_ids) == len(set(all_ids)) == 6
    assert offsets["internet_search"] == 6


def test_empty_call_leaves_earlier_ids_intact() -> None:
    offsets: dict = {}
    first = extract_citations_with_offset("internet_search", "tc1", _internet_result(2), offsets)
    # A call that yields no citations must not disturb the running offset.
    empty = extract_citations_with_offset("internet_search", "tc2", _internet_result(0), offsets)
    third = extract_citations_with_offset("internet_search", "tc3", _internet_result(1), offsets)
    assert [c.id for c in first] == ["internet_search-1", "internet_search-2"]
    assert empty == []
    assert [c.id for c in third] == ["internet_search-3"]
    assert offsets["internet_search"] == 3


def test_per_tool_offsets_independent() -> None:
    offsets: dict = {}
    a1 = extract_citations_with_offset("internet_search", "tc1", _internet_result(2), offsets)
    b1 = extract_citations_with_offset("get_industry_news", "tc2", _internet_result(2), offsets)
    a2 = extract_citations_with_offset("internet_search", "tc3", _internet_result(1), offsets)
    # Each tool keeps its own counter; the news call doesn't shift search ids.
    assert [c.id for c in a1] == ["internet_search-1", "internet_search-2"]
    assert [c.id for c in a2] == ["internet_search-3"]
    assert offsets["internet_search"] == 3
    # get_industry_news goes through the generic _news extractor; just assert
    # its ids are namespaced to its own tool and start fresh at 1.
    assert all(c.id.startswith("get_industry_news-") for c in b1)
    assert offsets["get_industry_news"] == len(b1)


def test_unparseable_id_suffix_is_skipped_not_crashed() -> None:
    """If a future extractor emits an id without a numeric suffix, the rewrite
    skips that item instead of raising (offset still advances by count)."""

    class _FakeCitation:
        def __init__(self, cid: str) -> None:
            self.id = cid

    import orchestration.citations as cm

    original = cm.extract_citations
    try:
        cm.extract_citations = lambda *a, **k: [_FakeCitation("internet_search-weird")]
        offsets = {"internet_search": 5}  # offset > 0 forces the rewrite branch
        items = extract_citations_with_offset("internet_search", "tc", {}, offsets)
        # Non-numeric suffix → left untouched, no exception.
        assert items[0].id == "internet_search-weird"
        assert offsets["internet_search"] == 6
    finally:
        cm.extract_citations = original

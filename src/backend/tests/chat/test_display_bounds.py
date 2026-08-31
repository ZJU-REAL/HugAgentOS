# -*- coding: utf-8 -*-
"""工具结果展示副本裁剪的契约测试。

这几条不是"覆盖率"，是这套裁剪能不能上线的**前提**：前端的知识库列表 / 搜索结果 /
通用列表卡片全靠字段名认结构，一旦裁剪动了键或数组长度，它们就会集体退化成裸 JSON。
"""
from core.chat.display_bounds import (
    DISPLAY_STRING_MAX,
    HISTORY_STRING_MAX,
    bound_result_for_display,
    bound_result_for_history,
)


def test_small_payload_is_returned_untouched():
    """常规大小的结果必须原样返回，连对象都不该新建——绝大多数调用走这条路。"""
    payload = {"ok": True, "file_id": "f1", "url": "/files/f1", "items": [{"title": "甲"}]}
    bounded, clipped = bound_result_for_display(payload)
    assert clipped is False
    assert bounded is payload


def test_long_string_is_clipped_but_structure_survives():
    payload = {
        "result": {
            "content": "中" * (DISPLAY_STRING_MAX * 3),
            "items": [{"title": "甲"}, {"title": "乙"}],
            "file_id": "f1",
        }
    }
    bounded, clipped = bound_result_for_display(payload)
    assert clipped is True
    # 键、层级、数组长度一个都不能少
    assert set(bounded["result"]) == {"content", "items", "file_id"}
    assert bounded["result"]["file_id"] == "f1"
    assert [item["title"] for item in bounded["result"]["items"]] == ["甲", "乙"]
    # 截了，而且明确写出了原始长度，不让用户以为这就是全部
    body = bounded["result"]["content"]
    assert len(body) > DISPLAY_STRING_MAX
    assert body.startswith("中" * 100)
    assert "完整内容共" in body


def test_input_is_never_mutated():
    original = {"content": "中" * (DISPLAY_STRING_MAX * 2)}
    before = original["content"]
    bound_result_for_display(original)
    assert original["content"] == before


def test_history_tier_is_much_tighter_than_live_tier():
    payload = {"content": "中" * (DISPLAY_STRING_MAX * 2)}
    live, _ = bound_result_for_display(payload)
    history, clipped = bound_result_for_history(payload)
    assert clipped is True
    assert len(history["content"]) < len(live["content"])
    assert history["content"].startswith("中" * HISTORY_STRING_MAX)


def test_total_budget_stops_many_medium_strings():
    """单个字段都不超限，但加起来能撑爆浏览器——总预算要能兜住这种形态。"""
    payload = {f"f{i}": "中" * 5_000 for i in range(200)}
    bounded, clipped = bound_result_for_history(payload)
    assert clipped is True
    assert len(bounded) == 200  # 字段一个不少
    assert sum(len(v) for v in bounded.values()) < sum(len(v) for v in payload.values())


def test_non_string_leaves_are_preserved():
    payload = {"n": 1, "f": 1.5, "b": False, "none": None, "list": [1, 2, 3]}
    bounded, clipped = bound_result_for_display(payload)
    assert clipped is False
    assert bounded == payload

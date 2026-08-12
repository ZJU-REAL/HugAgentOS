"""统一证据锚点（orchestration/citation_anchor.py）单元测试。

覆盖：发号器连续性、四层提取降级（__citations__ / 配置映射 / 启发式 / 整体兜底）、
JSON/纯文本回注、跳过名单、错误结果放行、collect_citation_dicts 双路径。
"""

import json

import pytest

from orchestration.citation_anchor import (
    CITATION_ALLOCATOR,
    SKIP_TOOLS,
    AnchorAllocator,
    anchor_start_for_chat,
    annotate_tool_result,
    attach_allocator,
    collect_citation_dicts,
    resolve_allocator,
)


@pytest.fixture(autouse=True)
def _clear_allocator_ctx():
    token = CITATION_ALLOCATOR.set(None)
    yield
    CITATION_ALLOCATOR.reset(token)


def _alloc(start: int = 1) -> AnchorAllocator:
    return AnchorAllocator(start)


# ── 发号器 ──────────────────────────────────────────────────────────────────


def test_allocator_sequences_across_tools():
    alloc = _alloc()
    assert alloc.next_id() == "e1"
    assert alloc.next_id() == "e2"
    alloc2 = _alloc(start=7)
    assert alloc2.next_id() == "e7"


def test_allocator_registry_by_tool_id():
    alloc = _alloc()
    text = json.dumps({"items": [{"title": "文档A", "content": "正文"}]}, ensure_ascii=False)
    _, items = annotate_tool_result("retrieve_local_kb", "call-1", text, alloc)
    alloc.register("call-1", items)
    got = alloc.citations_for("call-1")
    assert [c.id for c in got] == ["e1"]
    assert alloc.citations_for("call-other") == []


# ── 配置映射（L2）──────────────────────────────────────────────────────────


def test_internet_search_double_nested_list():
    alloc = _alloc()
    payload = {
        "result": {
            "query": "机器人",
            "results": [
                {"title": "报告A", "url": "https://a.com", "content": "内容A"},
                {"title": "报告B", "url": "https://b.com", "content": "内容B"},
            ],
        }
    }
    new_text, items = annotate_tool_result(
        "internet_search", "t1", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert [c.id for c in items] == ["e1", "e2"]
    assert items[0].title == "报告A"
    assert items[0].url == "https://a.com"
    assert items[0].source_type == "internet"
    assert items[0].item_index == 0 and items[1].item_index == 1
    annotated = json.loads(new_text)
    assert annotated["result"]["results"][0]["cite_id"] == "e1"
    assert annotated["result"]["results"][1]["cite_id"] == "e2"


def test_chinese_keys_kb_items():
    alloc = _alloc(start=5)
    payload = {"items": [{"文件名称": "政策文件", "文件内容": "第一条……", "document_id": "d1"}]}
    new_text, items = annotate_tool_result(
        "retrieve_dataset_content", "t2", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert items[0].id == "e5"
    assert items[0].title == "政策文件"
    assert items[0].snippet.startswith("第一条")
    assert json.loads(new_text)["items"][0]["cite_id"] == "e5"


def test_search_company_snippet_join():
    alloc = _alloc()
    payload = {"items": [{"企业名称": "某某科技", "法定代表人": "张三", "企业状态": "存续"}]}
    _, items = annotate_tool_result(
        "search_company", "t3", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert items[0].title == "某某科技"
    assert "张三" in items[0].snippet and "存续" in items[0].snippet


def test_whole_mode_spec():
    alloc = _alloc()
    payload = {"result": "✅ 查询成功\n\n[{\"a\": 1}]"}
    new_text, items = annotate_tool_result(
        "query_database", "t4", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert len(items) == 1
    assert items[0].id == "e1"
    assert items[0].title == "数据库查询结果"
    assert items[0].item_index == -1
    assert json.loads(new_text)["cite_id"] == "e1"


# ── 自声明（L1）────────────────────────────────────────────────────────────


def test_self_declared_citations_field():
    alloc = _alloc()
    payload = {
        "data": "whatever",
        "__citations__": [
            {"title": "来源甲", "url": "https://x.com", "snippet": "片段", "source_type": "internet"},
            {"title": "来源乙"},
        ],
    }
    new_text, items = annotate_tool_result(
        "some_new_tool", "t5", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert [c.id for c in items] == ["e1", "e2"]
    assert items[0].source_type == "internet"
    assert items[1].title == "来源乙"
    annotated = json.loads(new_text)
    assert annotated["__citations__"][0]["cite_id"] == "e1"
    assert annotated["__citations__"][1]["cite_id"] == "e2"


# ── 启发式（L3）与整体兜底（L4）────────────────────────────────────────────


def test_heuristic_unknown_tool_with_alias_list():
    alloc = _alloc()
    payload = {"ok": True, "items": [{"title": "条目1", "snippet": "s1"}, {"title": "条目2"}]}
    new_text, items = annotate_tool_result(
        "brand_new_tool", "t6", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert [c.id for c in items] == ["e1", "e2"]
    assert json.loads(new_text)["items"][0]["cite_id"] == "e1"


def test_heuristic_unique_dict_list_without_alias():
    alloc = _alloc()
    payload = {"total": 2, "hits": [{"name": "甲"}, {"name": "乙"}]}
    _, items = annotate_tool_result(
        "another_tool", "t7", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert len(items) == 2
    assert items[0].title == "甲"


def test_unknown_tool_whole_fallback():
    alloc = _alloc()
    payload = {"answer": "42", "unit": "无"}
    new_text, items = annotate_tool_result(
        "opaque_tool", "t8", json.dumps(payload, ensure_ascii=False), alloc
    )
    assert len(items) == 1
    assert items[0].item_index == -1
    assert json.loads(new_text)["cite_id"] == "e1"


def test_plain_text_result_footer():
    alloc = _alloc()
    new_text, items = annotate_tool_result("web_fetch_like_tool", "t9", "纯文本网页内容……", alloc)
    assert len(items) == 1
    assert new_text.endswith("[cite_id: e1]")
    assert items[0].snippet.startswith("纯文本")


def test_list_spec_with_plain_text_passthrough():
    alloc = _alloc()
    text = "not-json at all"
    new_text, items = annotate_tool_result("internet_search", "t10", text, alloc)
    assert new_text == text
    assert items == []


# ── 跳过与错误 ──────────────────────────────────────────────────────────────


def test_skip_tools_passthrough():
    alloc = _alloc()
    assert "pin_to_workspace" in SKIP_TOOLS and "Write" in SKIP_TOOLS
    text = json.dumps({"ok": True, "pinned": [{"file_id": "abc"}]})
    new_text, items = annotate_tool_result("pin_to_workspace", "t11", text, alloc)
    assert new_text == text and items == []


def test_error_result_not_annotated():
    alloc = _alloc()
    text = json.dumps({"error": "上游超时", "items": []}, ensure_ascii=False)
    new_text, items = annotate_tool_result("get_industry_news", "t12", text, alloc)
    assert new_text == text and items == []


def test_empty_list_result_not_annotated():
    alloc = _alloc()
    text = json.dumps({"items": []})
    new_text, items = annotate_tool_result("retrieve_local_kb", "t13", text, alloc)
    assert new_text == text and items == []


def test_annotate_never_raises_on_garbage():
    alloc = _alloc()
    new_text, items = annotate_tool_result("query_database", "t14", "", alloc)
    assert new_text == "" and items == []


# ── collect_citation_dicts 双路径 ───────────────────────────────────────────


def test_collect_reads_allocator_registry_from_contextvar():
    alloc = _alloc()
    CITATION_ALLOCATOR.set(alloc)
    text = json.dumps({"items": [{"title": "文档", "content": "x"}]}, ensure_ascii=False)
    _, items = annotate_tool_result("retrieve_local_kb", "call-9", text, alloc)
    alloc.register("call-9", items)
    got = collect_citation_dicts("call-9")
    assert [c["id"] for c in got] == ["e1"]
    assert got[0]["item_index"] == 0
    # 未注册的 tool_id：发号器是唯一编号方，取不到就是空（不做二次提取）
    assert collect_citation_dicts("call-x") == []


def test_collect_returns_empty_without_any_allocator():
    """发号器完全缺位（理论上不该发生）时返回空，而不是抛错或退回旧编号。"""
    assert collect_citation_dicts("call-1") == []


def test_anchor_start_without_chat_defaults_to_one():
    assert anchor_start_for_chat(None) == 1
    assert anchor_start_for_chat("") == 1


# ── agent 绑定通道（回归：ContextVar 跨 async-generator/task 不互通） ─────────


class _FakeAgent:
    """最小 agent 桩：只需要能挂属性。"""


def test_allocator_shared_via_agent_not_contextvar():
    """run 入口绑定在 agent 上后，中间件侧必须拿到**同一个**实例。

    回归点：编排层曾只写 ContextVar，而 agent 实际在另一个 task 上下文执行，
    导致中间件新建了自己的发号器、编排层读到 None 回退旧提取（引用 id 退化成
    `internet_search-1`，模型抄到的 cite_id 与之对不上）。
    """
    agent = _FakeAgent()
    run_alloc = AnchorAllocator(start=5)
    attach_allocator(agent, run_alloc)
    # 模拟中间件在另一个上下文里执行：ContextVar 被清空也必须仍取到同一个实例
    CITATION_ALLOCATOR.set(None)
    assert resolve_allocator(agent) is run_alloc
    assert resolve_allocator(agent).next_id() == "e5"


def test_resolve_allocator_creates_and_binds_when_missing():
    agent = _FakeAgent()
    alloc = resolve_allocator(agent)
    assert isinstance(alloc, AnchorAllocator)
    assert resolve_allocator(agent) is alloc  # 二次调用复用同一个


def test_middleware_registration_flow_end_to_end():
    """注号 → register → 按 tool_id 取回，链路必须闭合。

    回归点：中间件曾漏调 ``allocator.register()``，导致即便共享了发号器，
    ``collect_citation_dicts`` 仍取不到任何引用（SSE citations 为空）。
    """
    agent = _FakeAgent()
    alloc = attach_allocator(agent, AnchorAllocator())
    payload = {"result": {"results": [{"title": "A", "url": "u", "content": "c"}]}}

    # 中间件侧
    mw_alloc = resolve_allocator(agent)
    new_text, items = annotate_tool_result(
        "internet_search", "call-1", json.dumps(payload, ensure_ascii=False), mw_alloc
    )
    mw_alloc.register("call-1", items)
    assert json.loads(new_text)["result"]["results"][0]["cite_id"] == "e1"

    # 编排层侧：拿同一个 allocator 精确取
    got = collect_citation_dicts("call-1", alloc)
    assert [c["id"] for c in got] == ["e1"]


def test_second_call_continues_numbering_not_restart():
    """同一 run 内第二次调用同一工具必须接续编号，不得从 e1 重来。"""
    agent = _FakeAgent()
    alloc = attach_allocator(agent, AnchorAllocator())
    payload = {"result": {"results": [{"title": "A"}, {"title": "B"}]}}
    for call_id in ("call-1", "call-2"):
        _, items = annotate_tool_result(
            "internet_search", call_id, json.dumps(payload, ensure_ascii=False), alloc
        )
        alloc.register(call_id, items)
    assert [c.id for c in alloc.citations_for("call-1")] == ["e1", "e2"]
    assert [c.id for c in alloc.citations_for("call-2")] == ["e3", "e4"]

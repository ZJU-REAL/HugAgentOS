"""自建知识库 Wiki 生成管线的单元测试。

覆盖那些「错了很难被发现」的地方：

* slug 归一——同一事物写法不同会分裂成多个页面；
* 织链安全——往正文插链接时改坏代码块是很隐蔽的损坏；
* 引文短 ID 映射——映射错了血缘就断，回溯原文会取到别的分块；
* 去重的越界拒绝——把两件不同的事合成一页是**数据污染**，比漏合并严重得多；
* JSON 修复——推理模型的输出格式抽风不该让整批作业失败。
"""

from __future__ import annotations

import pytest

from core.kb.wiki import linkify, llm, slug as slug_mod
from core.kb.wiki.cite import (
    _parse_citations,
    apply_citations,
    render_chunks_xml,
    split_into_batches,
)
from core.kb.wiki.config import (
    MAX_CUSTOM_INSTRUCTIONS_LENGTH,
    WikiConfig,
    normalize_granularity,
    normalize_index_modes,
)
from core.kb.wiki.dedup import _similarity, apply_merges, select_candidates
from core.kb.wiki.extract import CandidateItem, parse_candidates
from core.kb.wiki.prompts import append_business_instructions
from core.kb.wiki.store import clean_category_path


class _Chunk:
    def __init__(self, chunk_id: str, content: str) -> None:
        self.chunk_id = chunk_id
        self.content = content


class _Page:
    def __init__(self, slug: str, title: str, page_type: str, aliases=None, summary="") -> None:
        self.slug = slug
        self.title = title
        self.page_type = page_type
        self.aliases = aliases or []
        self.summary = summary


# ── 索引模式 ────────────────────────────────────────────────────────────────


def test_index_modes_default_to_rag_for_legacy_spaces():
    """历史知识库的 metadata 里没有这个键，读出来必须是仅 RAG——
    它们确实已经建好了向量索引，误判成"什么都没建"会让检索整个失效。"""
    assert normalize_index_modes(None) == ["rag"]
    assert normalize_index_modes([]) == ["rag"]
    assert normalize_index_modes(["nonsense"]) == ["rag"]


def test_index_modes_accept_both_and_dedupe():
    assert normalize_index_modes(["wiki", "rag", "wiki"]) == ["wiki", "rag"]
    assert normalize_index_modes("wiki") == ["wiki"]


def test_wiki_config_clamps_out_of_range_values():
    """并发这些参数直接决定打给模型网关的压力，越界值必须被夹住而不是原样生效。"""
    config = WikiConfig.from_metadata(
        {"wiki_config": {"map_parallel": 9999, "citation_chunk_batch": 0, "granularity": "weird"}}
    )
    assert config.map_parallel == 16
    assert config.citation_chunk_batch == 1
    assert config.granularity == "standard"


def test_granularity_normalization():
    assert normalize_granularity("FOCUSED") == "focused"
    assert normalize_granularity(None) == "standard"


# ── slug ────────────────────────────────────────────────────────────────────


def test_normalize_slug_adds_missing_type_prefix():
    assert slug_mod.normalize_slug("acme-corp", default_type="entity") == "entity/acme-corp"
    assert slug_mod.normalize_slug("entity/Acme Corp") == "entity/acme-corp"


def test_normalize_slug_handles_fullwidth_slash_and_extra_depth():
    assert slug_mod.normalize_slug("entity／acme／corp") == "entity/acme-corp"


def test_summary_slug_keeps_document_id_verbatim():
    """摘要页 slug 的名字段是文档 ID，被 slugify 改写就再也对不回文档了。"""
    doc_id = "doc_A1b2C3d4"
    assert slug_mod.normalize_slug(f"summary/{doc_id}") == f"summary/{doc_id}"


def test_slug_handles_round_trip():
    """高熵 slug 换成短句柄给模型，输出后必须能原样还原。"""
    handles = slug_mod.SlugHandles(["summary/doc_deadbeef", "entity/acme"])
    handle = handles.handle_for("summary/doc_deadbeef")
    assert handle == "ref-1"

    restored = handles.restore(f"参见 [[{handle}|某文档]] 与 [[ref-2|Acme]]。")
    assert "[[summary/doc_deadbeef|某文档]]" in restored
    assert "[[entity/acme|Acme]]" in restored


def test_strip_links_except_removes_invented_and_self_links():
    content = "见 [[entity/real|真页]]、[[entity/fake|臆造]] 和 [[entity/self|本页]]。"
    cleaned = slug_mod.strip_links_except(
        content, ["entity/real", "entity/self"], self_slug="entity/self"
    )
    assert "[[entity/real|真页]]" in cleaned
    assert "[[entity/fake" not in cleaned and "臆造" in cleaned
    assert "[[entity/self" not in cleaned and "本页" in cleaned


# ── 织链安全 ────────────────────────────────────────────────────────────────


def test_linkify_never_touches_fenced_code():
    """代码块**排在正文之前**：只有真正跳过围栏，才会链到后面那处散文。

    若把代码块放在后面，「每个目标只链第一处」的规则会让测试即使在没有围栏保护
    时也通过——那就测不出任何东西了。
    """
    content = "```python\n# 示例科技 的配置\nx = 1\n```\n\n正文提到示例科技。\n"
    result = linkify.linkify_content(content, {"entity/acme": ["示例科技"]})

    code_block, _, prose = result.partition("```\n")
    assert "[[entity/acme" not in code_block, "代码块内的同名文本不得被改写"
    assert "# 示例科技 的配置" in code_block
    assert "[[entity/acme|示例科技]]" in prose, "跳过围栏后应链到正文那处"


def test_linkify_never_touches_code_only_content():
    """整篇只有代码时，一个链接都不该插入。"""
    content = "```\n示例科技\n```\n"
    assert linkify.linkify_content(content, {"entity/acme": ["示例科技"]}) == content


def test_linkify_never_touches_inline_code_or_existing_links():
    content = "配置项 `示例科技` 与已有链接 [示例科技](http://x) 以及 [[entity/acme|示例科技]]。"
    result = linkify.linkify_content(content, {"entity/acme": ["示例科技"]})
    assert result == content, "全部出现位置都在禁区内时应原样返回"


def test_linkify_skips_headings():
    """标题被改写会让目录锚点漂移。"""
    content = "## 示例科技 概览\n\n示例科技 是一家公司。"
    result = linkify.linkify_content(content, {"entity/acme": ["示例科技"]})
    assert result.startswith("## 示例科技 概览")
    assert "[[entity/acme|示例科技]] 是一家公司" in result


def test_linkify_links_each_target_at_most_once():
    content = "示例科技 很有名。示例科技 又出现了。示例科技 再次出现。"
    result = linkify.linkify_content(content, {"entity/acme": ["示例科技"]})
    assert result.count("[[entity/acme|") == 1


def test_linkify_prefers_longer_surface():
    """短名先匹配会把长名切碎，得到「[[短]]有限公司」这种残缺链接。"""
    content = "示例科技有限公司 是承建方。"
    result = linkify.linkify_content(
        content, {"entity/full": ["示例科技有限公司"], "entity/short": ["示例科技"]}
    )
    assert "[[entity/full|示例科技有限公司]]" in result
    assert "[[entity/short" not in result


def test_linkify_respects_ascii_word_boundary():
    content = "RAGTIME 不是 RAG 。"
    result = linkify.linkify_content(content, {"concept/rag": ["RAG"]})
    assert "RAGTIME" in result and "[[concept/rag|RAG]]" in result


# ── 引文标注 ────────────────────────────────────────────────────────────────


def test_batch_short_ids_are_globally_continuous():
    """短 ID 每批重置的话，多批结果合并回来就会撞号、把引文挂到错误的分块上。"""
    chunks = [_Chunk(f"real-{i}", f"内容{i}") for i in range(5)]
    batches = split_into_batches(chunks, 2)
    ids = [short for batch in batches for short, _ in batch]
    assert ids == ["c001", "c002", "c003", "c004", "c005"]


def test_chunks_xml_escapes_markup():
    """分块正文里的尖括号不转义会破坏提示词的 XML 结构。"""
    xml = render_chunks_xml([("c001", _Chunk("r1", "包含 <tag> 与 & 符号"))])
    assert "&lt;tag&gt;" in xml and "&amp;" in xml


def test_citations_parse_and_normalize_slugs():
    parsed = _parse_citations({"citations": {"acme": ["c001"], "concept/rag": ["c002", "c003"]}})
    assert parsed["concept/acme"] == ["c001"]
    assert parsed["concept/rag"] == ["c002", "c003"]


def test_apply_citations_drops_unsupported_items():
    """一条引文都拿不到的候选是模型的过度联想——留着只会产出没有出处的空壳页。"""
    items = [
        CandidateItem(slug="entity/a", name="甲", page_type="entity"),
        CandidateItem(slug="entity/b", name="乙", page_type="entity"),
    ]
    kept = apply_citations(items, {"entity/a": ["c1", "c2"]}, max_chunks=10)
    assert [i.slug for i in kept] == ["entity/a"]
    assert kept[0].chunk_ids == ["c1", "c2"]


def test_apply_citations_caps_chunk_count():
    items = [CandidateItem(slug="entity/a", name="甲", page_type="entity")]
    kept = apply_citations(items, {"entity/a": [f"c{i}" for i in range(30)]}, max_chunks=3)
    assert len(kept[0].chunk_ids) == 3


# ── 候选解析 ────────────────────────────────────────────────────────────────


def test_parse_candidates_splits_entities_and_concepts():
    parsed = parse_candidates(
        {
            "entities": [
                {"name": "示例科技", "slug": "entity/acme", "aliases": ["Acme", "示例科技"]}
            ],
            "concepts": [{"name": "检索增强生成", "slug": "concept/rag", "aliases": ["RAG"]}],
        }
    )
    by_slug = {item.slug: item for item in parsed}
    assert by_slug["entity/acme"].page_type == "entity"
    assert by_slug["concept/rag"].page_type == "concept"
    # 与本名重复的别名要剔掉，否则织链时会自己链自己
    assert by_slug["entity/acme"].aliases == ["Acme"]


def test_parse_candidates_ignores_malformed_entries():
    parsed = parse_candidates({"entities": [{"name": ""}, {"slug": "entity/x"}, "垃圾"]})
    assert parsed == []


# ── 去重 ────────────────────────────────────────────────────────────────────


def test_similarity_identifies_exact_and_rejects_unrelated():
    assert _similarity("示例科技", "示例科技") == 1.0
    assert _similarity("居民身份证", "驾驶证") < 0.34


def test_select_candidates_never_crosses_page_type():
    """实体与概念永远不合并：同名的公司和以它命名的方法论是两件事。"""
    item = CandidateItem(slug="entity/rag", name="RAG", page_type="entity")
    pages = [_Page("concept/rag", "RAG", "concept")]
    assert select_candidates(item, pages) == []


def test_apply_merges_folds_aliases_and_citations():
    """合并的意义在于两边证据汇总到一页；只改 slug 不搬证据等于丢数据。"""
    target = CandidateItem(
        slug="entity/acme", name="示例科技", page_type="entity", chunk_ids=["c1"]
    )
    source = CandidateItem(
        slug="entity/acme-corp",
        name="示例科技有限公司",
        page_type="entity",
        chunk_ids=["c2"],
    )
    merged = apply_merges([target, source], {"entity/acme-corp": "entity/acme"})

    assert [i.slug for i in merged] == ["entity/acme"]
    assert merged[0].chunk_ids == ["c1", "c2"]
    assert "示例科技有限公司" in merged[0].aliases


def test_apply_merges_is_noop_without_merges():
    items = [CandidateItem(slug="entity/a", name="甲", page_type="entity")]
    assert apply_merges(items, {}) == items


# ── 目录路径 ────────────────────────────────────────────────────────────────


def test_clean_category_path_drops_type_labels_and_caps_depth():
    """把「实体」当目录名是模型的常见错误——那是类型不是分类。"""
    assert clean_category_path(["实体", "组织", "地方机构", "第四级"]) == [
        "组织",
        "地方机构",
        "第四级",
    ]


def test_clean_category_path_normalizes_separators_and_dedupes():
    assert clean_category_path(["组织／下属单位", "组织"]) == ["组织", "下属单位"]


# ── LLM 输出健壮性 ──────────────────────────────────────────────────────────


def test_strip_think_blocks_handles_unclosed_tag():
    """输出被截断时 </think> 缺失，留着会让 JSON 提取从思考内容里捞出半截对象。"""
    assert llm.strip_think_blocks("<think>推理中…") == ""
    assert llm.strip_think_blocks("<think>x</think>正文") == "正文"


def test_parse_json_object_strips_fence_and_think():
    raw = '<think>想想</think>\n```json\n{"merges": {"a": "b"}}\n```'
    assert llm.parse_json_object(raw) == {"merges": {"a": "b"}}


def test_parse_json_object_repairs_literal_newlines_in_strings():
    """提示词禁止过，但模型仍会偶发违反，而这正是最常见的解析失败原因。"""
    raw = '{"details": "第一行\n第二行"}'
    parsed = llm.parse_json_object(raw)
    assert parsed is not None
    assert parsed["details"] == "第一行\n第二行"


def test_parse_json_object_extracts_from_surrounding_prose():
    raw = '好的，结果如下：\n{"citations": {"entity/a": ["c1"]}}\n希望有帮助。'
    assert llm.parse_json_object(raw) == {"citations": {"entity/a": ["c1"]}}


def test_parse_json_object_returns_none_on_garbage():
    """解析不出要返回 None 让调用方降级，而不是抛异常连累整批作业。"""
    assert llm.parse_json_object("完全不是 JSON") is None
    assert llm.parse_json_object("") is None


def test_split_summary_line():
    summary, body = llm.split_summary_line("SUMMARY: 一句话摘要\n# 标题\n正文")
    assert summary == "一句话摘要"
    assert body.startswith("# 标题")


def test_split_summary_line_tolerates_missing_marker():
    """模型偶尔忘了这一行；此时整段当正文，摘要留空由调用方兜底。"""
    summary, body = llm.split_summary_line("# 标题\n正文")
    assert summary == ""
    assert body.startswith("# 标题")


# ── 调用预算 ────────────────────────────────────────────────────────────────


def test_budget_raises_once_exhausted():
    """配额是设计好的刹车：一个超大库不该把整个额度打空。"""
    budget = llm.LLMBudget(max_calls=2)
    budget.check()
    budget.record(10, 10)
    budget.check()
    budget.record(10, 10)
    with pytest.raises(llm.WikiQuotaExceeded):
        budget.check()
    assert budget.snapshot()["llm_calls"] == 2


# ── 用户自定义业务要求 ──────────────────────────────────────────────────────


def test_business_instructions_are_appended_last_and_tagged():
    """自定义内容必须排在系统规则**之后**、装在自己的标签里。

    顺序不是风格问题：拼在前面等于让用户文本给系统规则定调。
    """
    result = append_business_instructions("系统规则正文", "多关注申报条件", "extraction")

    assert result.startswith("系统规则正文")
    assert "<extraction_business_instructions>" in result
    assert "多关注申报条件" in result
    assert result.index("系统规则正文") < result.index("多关注申报条件")


def test_business_instructions_declare_system_precedence():
    """必须显式声明冲突时以系统规则为准。

    少了这句，一条「不用标注出处」就能把整条接地链路废掉。
    """
    result = append_business_instructions("系统规则", "随便写写", "content")
    assert "以系统规则为准" in result


def test_business_instructions_noop_when_blank():
    assert append_business_instructions("系统规则", "", "content") == "系统规则"
    assert append_business_instructions("系统规则", "   \n ", "content") == "系统规则"


def test_custom_instructions_are_length_capped():
    """这是业务补充说明不是完整提示词；放任变长会挤占证据的上下文预算。"""
    config = WikiConfig.from_metadata(
        {
            "wiki_config": {
                "extraction_instructions": "很长" * 5000,
                "content_instructions": "  两端留白应被去掉  ",
            }
        }
    )
    assert len(config.extraction_instructions) == MAX_CUSTOM_INSTRUCTIONS_LENGTH
    assert config.content_instructions == "两端留白应被去掉"


def test_custom_instructions_default_to_empty():
    config = WikiConfig.from_metadata(None)
    assert config.extraction_instructions == ""
    assert config.content_instructions == ""

"""知识库索引模式与 Wiki 生成配置。

索引模式存在 ``kb_spaces.metadata.index_modes``，取值是 ``rag`` / ``wiki`` 的组合：

    ["rag"]            仅向量检索（历史库缺省即此）
    ["wiki"]           仅 Wiki 图谱
    ["rag", "wiki"]    LLM-Wiki 知识库

三种模式都会分块写 ``kb_chunks``——Wiki 的引文标注要 chunk id、回溯原文要按
chunk 直取，脱离分块无从谈起。``wiki`` 模式省掉的是 embedding 与 Milvus 写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

INDEX_MODE_RAG = "rag"
INDEX_MODE_WIKI = "wiki"
VALID_INDEX_MODES = (INDEX_MODE_RAG, INDEX_MODE_WIKI)

# 抽取粒度：直接决定候选条目数量，进而决定下游引文与写页的调用量
GRANULARITY_FOCUSED = "focused"
GRANULARITY_STANDARD = "standard"
GRANULARITY_EXHAUSTIVE = "exhaustive"
VALID_GRANULARITIES = (GRANULARITY_FOCUSED, GRANULARITY_STANDARD, GRANULARITY_EXHAUSTIVE)

# 用户自定义要求的长度上限。这是「业务补充说明」不是完整提示词——放任它变长会挤占
# 证据的上下文预算，也更容易和系统规则打架。
MAX_CUSTOM_INSTRUCTIONS_LENGTH = 4000

# 概念图谱单次返回的节点上限。这是前端渲染性能的闸，路由与实现层取同一个值。
MAX_GRAPH_NODES = 300


def normalize_index_modes(raw: Any) -> List[str]:
    """把任意输入归一成合法的索引模式列表；无法识别时回落 ``["rag"]``。

    历史知识库的 metadata 里没有这个键，落到这里就是缺省的仅 RAG——与它们
    实际已建好的索引一致，不会因为读出空值就误判成「什么都没建」。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return [INDEX_MODE_RAG]
    modes = []
    for item in raw:
        value = str(item or "").strip().lower()
        if value in VALID_INDEX_MODES and value not in modes:
            modes.append(value)
    return modes or [INDEX_MODE_RAG]


def normalize_granularity(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in VALID_GRANULARITIES else GRANULARITY_STANDARD


@dataclass
class WikiConfig:
    """单个知识库的 Wiki 生成参数。

    并发缺省值比「能跑多快」更保守：这些调用全部打在同一个模型网关上，和在线
    对话抢配额，跑满并发会直接拖慢用户正在进行的会话。
    """

    granularity: str = GRANULARITY_STANDARD
    language: str = "中文"
    # 单个作业最多合并处理多少篇文档（上传是一篇一个作业，处理时合批）
    batch_size: int = 5
    # Map 阶段（候选抽取 + 引文标注）并发
    map_parallel: int = 4
    # Reduce 阶段（写页）并发
    reduce_parallel: int = 4
    # 一批引文标注里塞多少个分块
    citation_chunk_batch: int = 8
    # 单个作业的 LLM 调用次数上限，防止一个超大库把额度打空
    max_llm_calls: int = 4000
    # 单页最多保留多少个引文分块（进 Reduce 提示词的证据量）
    max_chunks_per_page: int = 12
    # 用户自定义的业务要求。这两段会被追加到系统提示词末尾，用于表达「这个库里
    # 什么算重要」「页面该写成什么调子」——**不能**改写输出格式、引用与接地规则，
    # 系统规则始终优先（见 prompts.append_business_instructions）。
    extraction_instructions: str = ""  # 抽取侧：该重点关注哪些领域概念
    content_instructions: str = ""  # 撰写侧：语气、结构与详略取舍

    @classmethod
    def from_metadata(cls, metadata: Optional[Dict[str, Any]]) -> "WikiConfig":
        raw = {}
        if isinstance(metadata, dict):
            candidate = metadata.get("wiki_config")
            if isinstance(candidate, dict):
                raw = candidate

        def _text(key: str) -> str:
            return str(raw.get(key) or "").strip()[:MAX_CUSTOM_INSTRUCTIONS_LENGTH]

        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return cls(
            granularity=normalize_granularity(raw.get("granularity")),
            language=str(raw.get("language") or "中文").strip() or "中文",
            batch_size=_int("batch_size", 5, 1, 50),
            map_parallel=_int("map_parallel", 4, 1, 16),
            reduce_parallel=_int("reduce_parallel", 4, 1, 16),
            citation_chunk_batch=_int("citation_chunk_batch", 8, 1, 40),
            max_llm_calls=_int("max_llm_calls", 4000, 1, 100000),
            max_chunks_per_page=_int("max_chunks_per_page", 12, 1, 60),
            extraction_instructions=_text("extraction_instructions"),
            content_instructions=_text("content_instructions"),
        )


def index_modes_of(space: Any) -> List[str]:
    """读某个 KBSpace 行的索引模式。"""
    metadata = getattr(space, "extra_data", None)
    if not isinstance(metadata, dict):
        return [INDEX_MODE_RAG]
    return normalize_index_modes(metadata.get("index_modes"))


def wiki_enabled(space: Any) -> bool:
    return INDEX_MODE_WIKI in index_modes_of(space)


def rag_enabled(space: Any) -> bool:
    return INDEX_MODE_RAG in index_modes_of(space)


def resolve_wiki_config(space: Any) -> WikiConfig:
    return WikiConfig.from_metadata(getattr(space, "extra_data", None))


def index_modes_for_kb(db: Any, kb_id: str) -> List[str]:
    """按 kb_id 查索引模式。查不到或出错一律按仅 RAG——历史库就是这个语义。

    这是「某个库开了哪些索引」的唯一查询入口：软删谓词、缺省语义都只在这里定义
    一次，免得新增一个过滤条件时漏改某个副本。
    """
    if not kb_id:
        return [INDEX_MODE_RAG]
    return wiki_enabled_kb_ids_map(db, [kb_id]).get(kb_id, [INDEX_MODE_RAG])


def wiki_enabled_kb_ids_map(db: Any, kb_ids: Sequence[str]) -> Dict[str, List[str]]:
    """批量取 ``{kb_id: index_modes}``；查不到的 id 不出现在结果里。"""
    ids = [k for k in dict.fromkeys(kb_ids) if k]
    if not ids:
        return {}
    try:
        from core.db.models import KBSpace

        rows = (
            db.query(KBSpace.kb_id, KBSpace.extra_data)
            .filter(KBSpace.kb_id.in_(ids), KBSpace.deleted_at.is_(None))
            .all()
        )
    except Exception:
        return {}
    return {
        kb_id: normalize_index_modes((metadata or {}).get("index_modes"))
        for kb_id, metadata in rows
    }


def wiki_enabled_for_kb(db: Any, kb_id: str) -> bool:
    return INDEX_MODE_WIKI in index_modes_for_kb(db, kb_id)

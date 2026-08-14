"""把各阶段串成一次完整的 Wiki 摄入。

一次 ingest 作业处理一批文档：

    for 每篇文档:  Pass 0 候选抽取 → Pass 1..N 引文标注
    整批:          去重合并 → 目录规划
    for 每个条目:  Reduce 写页
    for 每篇文档:  写文档摘要页

并发用线程池而不是 asyncio：整条链路（requests 调模型、SQLAlchemy 同步会话）都是
阻塞式的，硬套 asyncio 只会到处 ``run_in_executor``。线程池的并发度直接对应
「同时打给模型网关的请求数」，语义更直白。

数据库会话的用法要留意：并发段里**不碰 session**——先把要用的分块内容读出来
放进内存，跑完再回主线程统一落库。SQLAlchemy 的 Session 不是线程安全的。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from core.db.models import KBChunk, KBDocument
from core.kb.wiki.cite import annotate_citations, apply_citations
from core.kb.wiki.config import WikiConfig
from core.kb.wiki.dedup import apply_merges, resolve_duplicates
from core.kb.wiki.extract import CandidateItem, extract_candidates
from core.kb.wiki.llm import LLMBudget, WikiQuotaExceeded
from core.kb.wiki.reduce import write_document_summary, write_entity_page
from core.kb.wiki.slug import summary_slug
from core.kb.wiki.store import (
    all_pages,
    archive_page,
    get_pages,
    pages_citing_document,
    upsert_page,
)
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 文档摘要页参考的正文上限
_MAX_SUMMARY_SOURCE_CHARS = 30000

ProgressFn = Callable[[str, int, int], None]


@dataclass
class _DocumentWork:
    """一篇文档在内存中的处理单元——脱离 session，可安全进线程池。"""

    document_id: str
    title: str
    chunks: List[Any] = field(default_factory=list)
    full_text: str = ""
    candidates: List[CandidateItem] = field(default_factory=list)


@dataclass
class IngestResult:
    documents: int = 0
    pages_written: int = 0
    summaries_written: int = 0
    quota_exceeded: bool = False


class _Chunk:
    """分块的只读快照。带过 session 的 ORM 实例进线程池不安全。"""

    __slots__ = ("chunk_id", "content", "document_id")

    def __init__(self, chunk_id: str, content: str, document_id: str) -> None:
        self.chunk_id = chunk_id
        self.content = content
        self.document_id = document_id


def _load_documents(db: Session, kb_id: str, document_ids: Sequence[str]) -> List[_DocumentWork]:
    """把待处理文档连同分块一次读进内存。"""
    if not document_ids:
        return []
    docs = (
        db.query(KBDocument)
        .filter(
            KBDocument.kb_id == kb_id,
            KBDocument.document_id.in_(list(document_ids)),
            KBDocument.deleted_at.is_(None),
        )
        .all()
    )
    if not docs:
        return []

    chunks = (
        db.query(KBChunk)
        .filter(KBChunk.kb_id == kb_id, KBChunk.document_id.in_([d.document_id for d in docs]))
        .order_by(KBChunk.document_id, KBChunk.chunk_index)
        .all()
    )
    by_doc: Dict[str, List[_Chunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.document_id, []).append(
            _Chunk(chunk.chunk_id, chunk.content or "", chunk.document_id)
        )

    works: List[_DocumentWork] = []
    for doc in docs:
        doc_chunks = by_doc.get(doc.document_id, [])
        if not doc_chunks:
            logger.info("Wiki 跳过无分块的文档 %s", doc.document_id)
            continue
        works.append(
            _DocumentWork(
                document_id=doc.document_id,
                title=doc.title or doc.filename or doc.document_id,
                chunks=doc_chunks,
                full_text="\n\n".join(c.content for c in doc_chunks if c.content),
            )
        )
    return works


def _existing_candidates_for(db: Session, kb_id: str, document_id: str) -> List[CandidateItem]:
    """该文档上一轮产出过哪些条目——用于强制 slug 复用。"""
    previous: List[CandidateItem] = []
    for page in pages_citing_document(db, kb_id, document_id):
        if page.page_type not in ("entity", "concept"):
            continue
        previous.append(
            CandidateItem(
                slug=page.slug,
                name=page.title or page.slug,
                page_type=page.page_type,
                description=page.summary or "",
                aliases=list(page.aliases or []),
            )
        )
    return previous


def _map_document(work: _DocumentWork, *, config: WikiConfig, budget: LLMBudget) -> _DocumentWork:
    """Map 阶段：一篇文档的候选抽取 + 引文标注。"""
    candidates = extract_candidates(
        content=work.full_text,
        config=config,
        budget=budget,
        previous=work.candidates,  # 调用前已被填成"上一轮的条目"
    )
    if not candidates:
        work.candidates = []
        return work

    citations, discovered = annotate_citations(
        candidates=candidates, chunks=work.chunks, config=config, budget=budget
    )
    merged_pool = {item.slug: item for item in candidates}
    for item in discovered:
        merged_pool.setdefault(item.slug, item)

    work.candidates = apply_citations(
        list(merged_pool.values()), citations, max_chunks=config.max_chunks_per_page
    )
    return work


def _collect_link_targets(
    db: Session, kb_id: str, batch_items: Sequence[CandidateItem]
) -> Dict[str, List[str]]:
    """可供织链的目标：全库既有页 + 本批条目。"""
    targets: Dict[str, List[str]] = {}
    for page in all_pages(db, kb_id, page_types=("entity", "concept")):
        targets[page.slug] = [page.title or page.slug, *(page.aliases or [])]
    for item in batch_items:
        targets[item.slug] = item.surfaces()
    return targets


def _source_contexts(works: Sequence[_DocumentWork]) -> str:
    """共享来源背景：说明这批素材各自出自什么文档。只用于校准语气，不是证据。"""
    lines = [f"- 《{work.title}》" for work in works[:20]]
    return "\n".join(lines)


def run_ingest(
    db: Session,
    kb_id: str,
    document_ids: Sequence[str],
    *,
    config: WikiConfig,
    budget: LLMBudget,
    progress: Optional[ProgressFn] = None,
) -> IngestResult:
    """跑一批文档的 Wiki 摄入。"""
    result = IngestResult()

    def _report(stage: str, done: int, total: int) -> None:
        if progress:
            try:
                progress(stage, done, total)
            except Exception:  # noqa: BLE001 - 进度上报失败不该影响生成
                logger.debug("Wiki 进度上报失败", exc_info=True)

    works = _load_documents(db, kb_id, document_ids)
    if not works:
        return result
    result.documents = len(works)

    for work in works:
        work.candidates = _existing_candidates_for(db, kb_id, work.document_id)

    # ── Map：抽取 + 引文（并发，不碰 session） ──────────────────────────────
    _report("抽取实体与引文", 0, len(works))
    try:
        with ThreadPoolExecutor(max_workers=config.map_parallel) as pool:
            futures = [pool.submit(_map_document, w, config=config, budget=budget) for w in works]
            for index, future in enumerate(futures, start=1):
                future.result()
                _report("抽取实体与引文", index, len(works))
    except WikiQuotaExceeded as exc:
        logger.warning("Wiki 生成触及调用上限: %s", exc)
        result.quota_exceeded = True

    batch_items: Dict[str, CandidateItem] = {}
    item_sources: Dict[str, List[str]] = {}
    for work in works:
        for item in work.candidates:
            existing = batch_items.get(item.slug)
            if existing is None:
                batch_items[item.slug] = item
            else:
                for chunk_id in item.chunk_ids:
                    if chunk_id not in existing.chunk_ids:
                        existing.chunk_ids.append(chunk_id)
                for alias in item.aliases:
                    if alias not in existing.aliases and alias != existing.name:
                        existing.aliases.append(alias)
            item_sources.setdefault(item.slug, []).append(work.document_id)
    if not batch_items:
        return result

    # ── 去重：与既有页比对合并 ──────────────────────────────────────────────
    if not result.quota_exceeded:
        try:
            existing_pages = all_pages(db, kb_id, page_types=("entity", "concept"))
            merges = resolve_duplicates(
                list(batch_items.values()), existing_pages, config=config, budget=budget
            )
            if merges:
                merged_items = apply_merges(list(batch_items.values()), merges)
                # 来源映射跟着合并走，否则被并掉的条目会丢掉血缘
                for source, target in merges.items():
                    item_sources.setdefault(target, []).extend(item_sources.pop(source, []))
                batch_items = {item.slug: item for item in merged_items}
        except WikiQuotaExceeded:
            result.quota_exceeded = True

    # 本批条目对应的既有页，一次性读进内存。下面的写页阶段跑在线程池里，
    # **绝不能**在那里碰 session——SQLAlchemy 的 Session 不是线程安全的，
    # 并发访问会直接炸成 "concurrent operations are not permitted"。
    existing_pages_by_slug = get_pages(db, kb_id, list(batch_items.keys()))
    existing_contents = {
        slug: (page.content or "") for slug, page in existing_pages_by_slug.items()
    }
    existing_folder_ids = {
        slug: (page.folder_id or "") for slug, page in existing_pages_by_slug.items()
    }

    # ── 目录规划：只给尚未归档的页分配 ──────────────────────────────────────
    assignments: Dict[str, List[str]] = {}
    if not result.quota_exceeded:
        from core.kb.wiki.taxonomy import plan_taxonomy

        unfiled = [item for item in batch_items.values() if not existing_folder_ids.get(item.slug)]
        try:
            assignments = plan_taxonomy(db, kb_id, unfiled, config=config, budget=budget)
        except WikiQuotaExceeded:
            result.quota_exceeded = True

    # ── Reduce：写页 ────────────────────────────────────────────────────────
    link_targets = _collect_link_targets(db, kb_id, list(batch_items.values()))
    chunk_index = {c.chunk_id: c for work in works for c in work.chunks}
    contexts = _source_contexts(works)

    payloads: List[Dict[str, Any]] = []
    items = list(batch_items.values())
    _report("撰写条目页", 0, len(items))

    def _write(item: CandidateItem) -> Optional[Dict[str, Any]]:
        """只读内存快照，不碰 session——本函数跑在线程池里。"""
        evidence = [chunk_index[cid] for cid in item.chunk_ids if cid in chunk_index]
        return write_entity_page(
            item=item,
            chunks=evidence,
            existing_content=existing_contents.get(item.slug, ""),
            link_targets={k: v for k, v in link_targets.items() if k != item.slug},
            source_contexts=contexts,
            config=config,
            budget=budget,
        )

    try:
        with ThreadPoolExecutor(max_workers=config.reduce_parallel) as pool:
            futures = [pool.submit(_write, item) for item in items]
            for index, future in enumerate(futures, start=1):
                payload = future.result()
                if payload:
                    payloads.append(payload)
                _report("撰写条目页", index, len(items))
    except WikiQuotaExceeded:
        result.quota_exceeded = True

    for payload in payloads:
        slug = payload["slug"]
        item = batch_items.get(slug)
        upsert_page(
            db,
            kb_id,
            slug=slug,
            title=payload["title"],
            page_type=payload["page_type"],
            content=payload["content"],
            summary=payload["summary"],
            aliases=payload["aliases"],
            source_refs=item_sources.get(slug, []),
            chunk_refs=(item.chunk_ids if item else []),
            out_links=payload["out_links"],
            category_path=assignments.get(slug),
        )
    result.pages_written = len(payloads)
    db.commit()

    # ── 文档摘要页 ──────────────────────────────────────────────────────────
    if not result.quota_exceeded:
        result.summaries_written = _write_summaries(
            db, kb_id, works, link_targets, config=config, budget=budget, report=_report
        )

    return result


def _write_summaries(
    db: Session,
    kb_id: str,
    works: Sequence[_DocumentWork],
    link_targets: Dict[str, List[str]],
    *,
    config: WikiConfig,
    budget: LLMBudget,
    report: ProgressFn,
) -> int:
    report("撰写文档摘要", 0, len(works))

    def _summarize(work: _DocumentWork) -> Optional[Dict[str, Any]]:
        return write_document_summary(
            slug=summary_slug(work.document_id),
            title=work.title,
            content=work.full_text[:_MAX_SUMMARY_SOURCE_CHARS],
            link_targets=link_targets,
            config=config,
            budget=budget,
        )

    payloads: List[tuple[_DocumentWork, Dict[str, Any]]] = []
    try:
        with ThreadPoolExecutor(max_workers=config.map_parallel) as pool:
            futures = {pool.submit(_summarize, work): work for work in works}
            for index, (future, work) in enumerate(futures.items(), start=1):
                payload = future.result()
                if payload:
                    payloads.append((work, payload))
                report("撰写文档摘要", index, len(works))
    except WikiQuotaExceeded:
        logger.warning("Wiki 文档摘要阶段触及调用上限")

    for work, payload in payloads:
        upsert_page(
            db,
            kb_id,
            slug=payload["slug"],
            title=work.title,
            page_type="summary",
            content=payload["content"],
            summary=payload["summary"],
            source_refs=[work.document_id],
            chunk_refs=[c.chunk_id for c in work.chunks[:20]],
            out_links=payload["out_links"],
            category_path=[],
        )
    db.commit()
    return len(payloads)


def run_retract(db: Session, kb_id: str, document_ids: Sequence[str]) -> Dict[str, int]:
    """文档被删后，把它从 Wiki 血缘里摘掉。

    这一步不调 LLM：只摘引用、只归档失去全部来源的页。正文里残留的相关段落交给
    下一次 ingest 的增量合并去修——为一次删除把整批页面重写一遍代价太高，而且
    没有新素材时模型也改不出更好的版本。

    整批一次算完：受影响的页面扫一遍库，存活分块用一条 IN 查询解决。原来是每个
    文档扫一次全表、每个页面再查一次分块。
    """
    removed = {str(d) for d in document_ids if d}
    if not removed:
        return {"touched": 0, "archived": 0}

    pages = [page for page in all_pages(db, kb_id) if removed.intersection(page.source_refs or [])]
    if not pages:
        return {"touched": 0, "archived": 0}

    alive = _surviving_chunk_ids(db, kb_id, {c for p in pages for c in (p.chunk_refs or [])})

    archived = 0
    for page in pages:
        sources = [s for s in (page.source_refs or []) if s not in removed]
        page.source_refs = sources
        # 只摘掉随该文档消失的分块。整列清空会连带丢掉其他来源文档的血缘，
        # 那些页面之后就再也回溯不到原文了。
        page.chunk_refs = [c for c in (page.chunk_refs or []) if c in alive]
        if not sources:
            archive_page(db, page)
            archived += 1
    db.commit()
    return {"touched": len(pages), "archived": archived}


def _surviving_chunk_ids(db: Session, kb_id: str, chunk_ids: Set[str]) -> Set[str]:
    """仍然存在的分块 ID 集合。文档删除时分块级联消失，剩下的就是悬空引用。"""
    if not chunk_ids:
        return set()
    rows = (
        db.query(KBChunk.chunk_id)
        .filter(KBChunk.kb_id == kb_id, KBChunk.chunk_id.in_(list(chunk_ids)))
        .all()
    )
    return {row.chunk_id for row in rows}

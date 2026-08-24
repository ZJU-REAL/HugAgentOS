"""Knowledge base document processing — chunking, keyword extraction, vectorisation.

Background task logic extracted from ``api/routes/v1/kb.py``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── Text helpers ────────────────────────────────────────────────────────────

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks and markdown formatting from LLM output."""
    # Remove <think>...</think> blocks (reasoning model output)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Remove markdown bold/italic markers
    text = re.sub(r'\*+', '', text)
    # Remove markdown headers
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    # Remove bullet points
    text = re.sub(r'^[\-\*]\s*', '', text, flags=re.MULTILINE)
    return text.strip()


# ── Model config helper ────────────────────────────────────────────────────

def resolve_main_model_config() -> tuple[str, str, str]:
    """Resolve main_agent model config from DB, with env fallback."""
    try:
        from core.services.model_config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("main_agent")
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.api_key, cfg.model_name
    except Exception:
        pass
    return (
        os.getenv("MODEL_URL", "").rstrip("/"),
        os.getenv("API_KEY", ""),
        os.getenv("BASE_MODEL_NAME", ""),
    )


# ── LLM-powered enrichment ─────────────────────────────────────────────────

def extract_keywords_llm(content: str, count: int) -> list[str]:
    """Call configured LLM to extract keywords from a text chunk."""
    import requests as _requests

    model_url, api_key, model_name = resolve_main_model_config()
    if not model_url or not model_name:
        return []
    try:
        resp = _requests.post(
            f"{model_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": (
                        "你是关键词提取工具。"
                        "输出规则：只输出关键词，用英文逗号分隔，不要编号，不要解释，不要分析过程。"
                        "示例输出：数字化转型,产业升级,人工智能"
                    )},
                    {"role": "user", "content": f"从以下文本提取{count}个关键词，直接输出逗号分隔的关键词：\n\n{content[:2000]}"},
                ],
                "temperature": 0.1,
                "max_tokens": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = strip_think_tags(raw)
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        best_line = raw
        for line in reversed(lines):
            if ("," in line or "，" in line) and len(line) < 500:
                best_line = line
                break
        keywords = [kw.strip() for kw in best_line.replace("，", ",").split(",") if kw.strip() and len(kw.strip()) < 30]
        return keywords[:count]
    except Exception as exc:
        logger.warning("LLM keyword extraction failed: %s", exc)
        return []


def generate_questions_llm(content: str, count: int) -> list[str]:
    """Call configured LLM to generate retrieval questions for a text chunk."""
    import requests as _requests

    model_url, api_key, model_name = resolve_main_model_config()
    if not model_url or not model_name:
        return []
    try:
        resp = _requests.post(
            f"{model_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": (
                        "你是问题生成工具。"
                        "输出规则：每行一个问题，不要编号，不要解释，不要分析过程。直接输出问题本身。"
                        "示例输出：\n什么是数字化转型？\n如何申报产业升级项目？"
                    )},
                    {"role": "user", "content": f"根据以下文本生成{count}个检索问题，每行一个：\n\n{content[:2000]}"},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = strip_think_tags(raw)
        questions = []
        for q in raw.split("\n"):
            q = q.strip().lstrip("0123456789.、）)-— ").strip()
            if not q or len(q) < 5:
                continue
            if q.startswith(("分析", "角色", "任务", "输出", "规则", "示例")):
                continue
            questions.append(q)
        return questions[:count]
    except Exception as exc:
        logger.warning("LLM question generation failed: %s", exc)
        return []


# ── Background vectorisation ───────────────────────────────────────────────

def update_document_status(document_id: str, status: str, error: Optional[str] = None) -> None:
    """Update the indexing_status of a document in the DB.

    On failure, write the error reason into extra_data (the metadata JSON column) so the frontend/admin console can see the
    failure reason without digging through logs; for non-failure statuses, clear the historical error marker to avoid a stale error lingering after a successful re-index.
    """
    try:
        from datetime import datetime
        from sqlalchemy.orm.attributes import flag_modified

        from core.db.engine import SessionLocal
        from core.db.models import KBDocument
        db = SessionLocal()
        try:
            doc = db.query(KBDocument).filter(KBDocument.document_id == document_id).first()
            if doc:
                doc.indexing_status = status
                meta = dict(doc.extra_data or {})
                if status == "failed" and error:
                    meta["indexing_error"] = error[:1000]
                    meta["indexing_failed_at"] = datetime.utcnow().isoformat()
                else:
                    meta.pop("indexing_error", None)
                    meta.pop("indexing_failed_at", None)
                # 终态就不再是任何 worker 的在飞任务了，认领痕迹一并清掉，
                # 免得下次重排时被僵尸回收逻辑看见半截旧心跳
                meta.pop("indexing_claimed_by", None)
                meta.pop("indexing_heartbeat", None)
                doc.extra_data = meta
                flag_modified(doc, "extra_data")
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Failed to update document status: %s", exc)


def vectorise_document_background(
    document_id: str,
    kb_id: str,
    user_id: str,
    title: str,
    file_bytes: bytes,
    mime_type: str,
    chunk_method: str,
    db_url: str,
    indexing_config: Optional[dict] = None,
    index_modes: Optional[list[str]] = None,
    loop=None,
) -> None:
    """Background task: parse document -> build parent-child chunks -> write to Milvus + DB.

    ``loop``：调度这次索引的宿主事件循环，由常驻 worker 传入（见
    ``core/kb/index_worker.py``）。索引跑在 worker 自己的线程池里——那不是 anyio 的
    工作线程，没有回到宿主循环的通路，所以图像理解需要显式拿到它。经 BackgroundTasks
    进来的调用方不传，走 anyio 那条路即可。

    按知识库的索引模式分两路：``rag`` 决定要不要向量化并写 Milvus，``wiki`` 决定
    索引完成后要不要排 Wiki 生成作业。**分块（KBChunk）两种模式都要写**——Wiki 的
    引文标注按 chunk 走、回溯原文按 chunk 直取，脱离分块无从谈起。
    """
    try:
        from core.kb.kb_assets import multimodal_indexing_enabled
        from core.kb.kb_parser import parse_and_chunk_rich
        from core.kb.kb_vector import (
            get_or_create_collection, embed_batch, build_sparse_text, text_to_sparse,
            upsert_rows, upsert_question_rows, truncate_utf8,
            TITLE_FIELD_MAX_BYTES, TAGS_FIELD_MAX_BYTES,
        )
        from core.db.engine import SessionLocal
        from core.db.models import KBChunk

        # 调用方手上都有 KBSpace，能传就传：批量上传时每篇再开一次 session 去
        # 重读同一行，会和并发的索引任务一起把连接池顶满（实测 39 篇并发上传
        # 时出现 QueuePool 超时导致索引失败）。
        if index_modes is None:
            index_modes = _resolve_index_modes(kb_id)
        want_vectors = "rag" in index_modes
        want_wiki = "wiki" in index_modes

        cfg = indexing_config or {}
        parent_size = cfg.get("parent_chunk_size", 1024)
        child_size = cfg.get("child_chunk_size", 128)
        overlap = cfg.get("overlap_tokens", 20)
        parent_child = cfg.get("parent_child_indexing", True)
        auto_keywords = cfg.get("auto_keywords_count", 0)
        auto_questions = cfg.get("auto_questions_count", 0)
        separators = cfg.get("separators") or None
        child_separators = cfg.get("child_separators") or None

        logger.info(
            "Indexing config for document %s: parent_size=%d, child_size=%d, overlap=%d, "
            "parent_child=%s, auto_keywords=%d, auto_questions=%d, parent_seps=%s, child_seps=%s",
            document_id, parent_size, child_size, overlap, parent_child, auto_keywords,
            auto_questions, bool(separators), bool(child_separators),
        )

        _embed_fn = embed_batch if chunk_method == "embedding_semantic" else None
        # 多模态：``rag`` 模式下才抽图——仅 Wiki 的库不做检索，多花一次重解析没有收益。
        _want_assets = want_vectors and multimodal_indexing_enabled(cfg)
        parent_chunks, raw_assets = parse_and_chunk_rich(
            file_bytes, mime_type, chunk_method=chunk_method,
            parent_size=parent_size, child_size=child_size, overlap=overlap,
            embed_fn=_embed_fn, separators=separators, child_separators=child_separators,
            with_assets=_want_assets,
        )
        logger.info("Parsed %d parent chunks for document %s", len(parent_chunks), document_id)

        if not parent_chunks:
            # 解析不出任何内容（空文件、纯图片 PDF、不认识的格式…）也必须落终态：
            # 直接 return 会让文档永远停在「索引中」，前端一直转圈且无从重试。
            logger.warning("No chunks produced for document %s", document_id)
            update_document_status(
                document_id, "failed", error="未能从该文件解析出任何文本内容，请确认文件格式与内容"
            )
            return

        # 资产必须在向量化之前落库：它会把父块正文里的图片占位符改写成真实资产 URL，
        # 而入库的分块正文与被向量化的文本必须是同一份。
        stored_asset_ids = _persist_document_assets(
            kb_id=kb_id,
            document_id=document_id,
            user_id=user_id,
            assets=raw_assets,
            parents=parent_chunks,
        )

        dense_vecs: list = []
        if want_vectors:
            get_or_create_collection()
            if parent_child:
                embed_texts = [child.content for pc in parent_chunks for child in pc.children]
            else:
                embed_texts = [pc.content for pc in parent_chunks]
            dense_vecs = embed_batch(embed_texts)
        else:
            # 仅 Wiki 模式：分块照写，跳过向量化与 Milvus 写入
            logger.info(
                "Document %s: kb %s 未启用向量检索，跳过向量化（index_modes=%s）",
                document_id, kb_id, index_modes,
            )

        milvus_rows: list[dict] = []
        vec_idx = 0

        db = SessionLocal()
        try:
            for pc_idx, pc in enumerate(parent_chunks):
                chunk_tags: list[str] = []
                if auto_keywords > 0:
                    chunk_tags = extract_keywords_llm(pc.content, auto_keywords)

                chunk_questions: list[str] = []
                if auto_questions > 0:
                    chunk_questions = generate_questions_llm(pc.content, auto_questions)

                db_chunk = KBChunk(
                    chunk_id=pc.parent_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    chunk_index=pc_idx,
                    content=pc.content,
                    tags=chunk_tags,
                    questions=chunk_questions,
                    char_start=pc.char_start,
                    char_end=pc.char_end,
                )
                db.add(db_chunk)

                if not want_vectors:
                    continue

                if parent_child:
                    for child in pc.children:
                        tags_for_bm25 = chunk_tags if chunk_tags else []
                        sparse_text = build_sparse_text(child.content, tags_for_bm25)
                        milvus_rows.append({
                            "chunk_id":        child.child_id,
                            "parent_chunk_id": pc.parent_id,
                            "row_type":        "chunk",
                            "user_id":         user_id,
                            "kb_id":           kb_id,
                            "document_id":     document_id,
                            "title":           truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
                            "content":         truncate_utf8(child.content),
                            "tags_text":       truncate_utf8(" ".join(tags_for_bm25), TAGS_FIELD_MAX_BYTES),
                            "chunk_index":     child.index,
                            "dense_embedding": dense_vecs[vec_idx],
                            "sparse_embedding": text_to_sparse(sparse_text),
                        })
                        vec_idx += 1
                else:
                    tags_for_bm25 = chunk_tags if chunk_tags else []
                    sparse_text = build_sparse_text(pc.content, tags_for_bm25)
                    milvus_rows.append({
                        "chunk_id":        pc.parent_id,
                        "parent_chunk_id": pc.parent_id,
                        "row_type":        "chunk",
                        "user_id":         user_id,
                        "kb_id":           kb_id,
                        "document_id":     document_id,
                        "title":           truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
                        "content":         truncate_utf8(pc.content),
                        "tags_text":       truncate_utf8(" ".join(tags_for_bm25), TAGS_FIELD_MAX_BYTES),
                        "chunk_index":     pc_idx,
                        "dense_embedding": dense_vecs[vec_idx],
                        "sparse_embedding": text_to_sparse(sparse_text),
                    })
                    vec_idx += 1

            db.commit()
            logger.info("Saved %d KBChunk records for document %s", len(parent_chunks), document_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        BATCH = 100
        for i in range(0, len(milvus_rows), BATCH):
            upsert_rows(milvus_rows[i:i + BATCH])
        logger.info("Upserted %d chunk vectors to Milvus for document %s", len(milvus_rows), document_id)

        if auto_questions > 0 and want_vectors:
            db2 = SessionLocal()
            try:
                chunks_with_questions = db2.query(KBChunk).filter(
                    KBChunk.document_id == document_id,
                    KBChunk.kb_id == kb_id,
                ).all()
                for c in chunks_with_questions:
                    if c.questions:
                        try:
                            upsert_question_rows(
                                parent_chunk_id=c.chunk_id,
                                questions=c.questions,
                                user_id=user_id,
                                kb_id=kb_id,
                                document_id=document_id,
                                title=title,
                                chunk_index=c.chunk_index,
                            )
                        except Exception as q_exc:
                            logger.warning("Question row upsert failed for chunk %s: %s", c.chunk_id, q_exc)
            finally:
                db2.close()
            logger.info("Upserted question rows for document %s", document_id)

        # 图生文 + 资产检索行。放在文本索引之后：VLM 调用慢且可能没配，文本检索不该等它。
        if stored_asset_ids and want_vectors:
            _index_document_assets(
                loop=loop,
                asset_ids=stored_asset_ids,
                kb_id=kb_id,
                document_id=document_id,
                user_id=user_id,
                title=title,
            )

        update_document_status(document_id, "completed")
        logger.info("Indexing completed for document %s", document_id)

        if want_wiki:
            _enqueue_wiki_ingest(kb_id, document_id)

    except Exception as exc:
        logger.error("Background vectorisation failed for document %s: %s", document_id, exc, exc_info=True)
        update_document_status(document_id, "failed", error=f"{type(exc).__name__}: {exc}")


def _persist_document_assets(
    *,
    kb_id: str,
    document_id: str,
    user_id: str,
    assets: list,
    parents: list,
) -> list[str]:
    """落盘文档抽出的媒体资产，返回其 id。失败只记日志——正文索引不能被一张图拖垮。

    只回 id 不回 ORM 行：session 一关行就 detach，后续任何属性访问都可能炸在
    「已过期属性重新加载」上。下游本来也只需要 id 去开新 session 重查。
    """
    if not assets:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.kb.kb_assets import persist_assets

        with SessionLocal() as db:
            created = persist_assets(
                db,
                kb_id=kb_id,
                document_id=document_id,
                user_id=user_id,
                assets=assets,
                parents=parents,
            )
            return [row.asset_id for row in created]
    except Exception as exc:
        logger.warning("文档 %s 资产落盘失败，仅索引正文: %s", document_id, exc)
        return []


def _index_document_assets(
    *,
    loop,
    asset_ids: list,
    kb_id: str,
    document_id: str,
    user_id: str,
    title: str,
) -> None:
    """给资产生成描述并写入检索行。整段失败不影响已完成的正文索引。"""
    if not asset_ids:
        return
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBAsset
        from core.kb.kb_assets import generate_captions, index_assets, mark_vector_state

        with SessionLocal() as db:
            rows = db.query(KBAsset).filter(KBAsset.asset_id.in_(asset_ids)).all()
            if not rows:
                return
            generate_captions(db, rows, loop=loop)
            written = index_assets(
                rows, user_id=user_id, kb_id=kb_id, document_id=document_id, title=title
            )
            if written:
                mark_vector_state(db, rows, "text", "ok")
    except Exception as exc:
        logger.warning("文档 %s 资产索引失败，图片不可检索: %s", document_id, exc)


def _resolve_index_modes(kb_id: str) -> list[str]:
    """读该知识库的索引模式。查不到按仅 RAG 处理——历史库都是这个语义。"""
    try:
        from core.db.engine import SessionLocal
        from core.kb.wiki.config import index_modes_for_kb

        with SessionLocal() as db:
            return index_modes_for_kb(db, kb_id)
    except Exception as exc:
        logger.warning("解析知识库 %s 的索引模式失败，按仅 RAG 处理: %s", kb_id, exc)
        return ["rag"]


def _enqueue_wiki_ingest(kb_id: str, document_id: str) -> None:
    """索引完成后把文档排进 Wiki 生成队列。

    入队失败不能反过来把文档判成索引失败——分块和向量都已经写好了，检索本身是
    可用的，Wiki 缺了可以之后用重新生成接口补。
    """
    try:
        from core.db.engine import SessionLocal
        from core.kb.wiki.jobs import enqueue_ingest

        with SessionLocal() as db:
            enqueue_ingest(db, kb_id, [document_id])
    except Exception as exc:
        logger.warning("文档 %s 排入 Wiki 生成队列失败: %s", document_id, exc)

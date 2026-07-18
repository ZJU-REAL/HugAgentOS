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
) -> None:
    """Background task: parse document -> build parent-child chunks -> write to Milvus + DB."""
    try:
        from core.kb.kb_parser import parse_and_chunk
        from core.kb.kb_vector import (
            get_or_create_collection, embed_batch, build_sparse_text, text_to_sparse,
            upsert_rows, upsert_question_rows, truncate_utf8,
            TITLE_FIELD_MAX_BYTES, TAGS_FIELD_MAX_BYTES,
        )
        from core.db.engine import SessionLocal
        from core.db.models import KBChunk

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
        parent_chunks = parse_and_chunk(
            file_bytes, mime_type, chunk_method=chunk_method,
            parent_size=parent_size, child_size=child_size, overlap=overlap,
            embed_fn=_embed_fn, separators=separators, child_separators=child_separators,
        )
        logger.info("Parsed %d parent chunks for document %s", len(parent_chunks), document_id)

        get_or_create_collection()

        if parent_child:
            embed_texts = [child.content for pc in parent_chunks for child in pc.children]
        else:
            embed_texts = [pc.content for pc in parent_chunks]

        if not embed_texts:
            logger.warning("No chunks produced for document %s", document_id)
            return

        dense_vecs = embed_batch(embed_texts)

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

        if auto_questions > 0:
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

        update_document_status(document_id, "completed")
        logger.info("Indexing completed for document %s", document_id)

    except Exception as exc:
        logger.error("Background vectorisation failed for document %s: %s", document_id, exc, exc_info=True)
        update_document_status(document_id, "failed", error=f"{type(exc).__name__}: {exc}")

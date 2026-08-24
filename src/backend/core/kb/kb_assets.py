"""Knowledge-base media assets: persistence, 图生文, and their retrieval rows.

The text pipeline is unchanged — this module is the "路径 A" layer on top of it:

    parse → parents + RawAsset[]        (core/kb/kb_parser.py)
      → persist_assets()                bytes to object storage, rows to kb_assets,
                                        placeholders in the parent text rewritten to
                                        real URLs, each asset linked to its parent chunk
      → generate_captions()             图像理解走 core/vision 视觉桥（与对话同一条通路）
      → index_assets()                  一条 ``row_type="image"``
                                        row per asset in the existing Milvus collection

Because the image rows carry ``parent_chunk_id`` of the chunk that contains them, every
downstream behaviour — dedup by parent, parent content fetched from PostgreSQL, RRF,
reranking, per-document deletion — keeps working with no changes.

Two extension seams are deliberate:

* **路径 B（真多模态向量）** — a CLIP-family embedding cannot share this collection: the
  dimension differs and ``get_or_create_collection`` drops the collection on a dimension
  mismatch. It belongs in its own collection, recorded per asset under
  ``KBAsset.vector_state["visual"]``, and fused with these results in Python.
* **音视频** — ``KBAsset.kind`` and ``RawAsset.kind`` already widen to audio/video. The
  medium-specific work is producing ``text_content`` (ASR transcript) and ``caption``;
  everything from :func:`index_assets` onwards is medium-agnostic.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Iterable, Optional, Sequence

from core.kb.kb_parser import RawAsset, image_extension_for_mime

logger = logging.getLogger(__name__)

# Milvus ``row_type`` for a media asset row (alongside "chunk" / "question").
ROW_TYPE_IMAGE = "image"

# Milvus primary keys must not collide with chunk ids; asset rows are namespaced.
_ASSET_ROW_PREFIX = "a_"

# Images below this many bytes are almost always icons / rules / bullet glyphs that the
# layout engine cut out. Describing them wastes a vision call per item and pollutes
# retrieval with near-empty descriptions.
_MIN_ASSET_BYTES = int(os.getenv("KB_ASSET_MIN_BYTES", "4096"))
# 单文档图像理解张数上限，防一篇图海文档独占视觉桥。体积上限、单次调用超时都归视觉桥
# 自己管（``VISION_MAX_IMAGE_BYTES`` / ``VISION_CALL_TIMEOUT_SECONDS``），这里不再重复设。
_MAX_CAPTIONS_PER_DOC = int(os.getenv("KB_ASSET_CAPTION_MAX_PER_DOC", "200"))
# Minimum pixel edge; only enforced when Pillow is importable (it arrives transitively
# via python-pptx, so it is treated as optional).
_MIN_ASSET_EDGE_PX = int(os.getenv("KB_ASSET_MIN_EDGE_PX", "64"))



def multimodal_indexing_enabled(indexing_config: Optional[dict]) -> bool:
    """Whether to extract and index media for this document.

    Per-KB opt-out via ``indexing_config.multimodal_indexing``; the global default
    lives in ``KB_MULTIMODAL_INDEXING`` so an operator can turn the extra parse cost
    off fleet-wide without touching each knowledge base.
    """
    if isinstance(indexing_config, dict) and indexing_config.get("multimodal_indexing") is not None:
        return bool(indexing_config.get("multimodal_indexing"))
    return (os.getenv("KB_MULTIMODAL_INDEXING", "true") or "").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def asset_public_url(asset_id: str) -> str:
    """Frontend-resolvable URL for an asset. Matches the route in ``api/routes/v1/kb.py``.

    The ``/api`` prefix mirrors the frontend's ``VITE_API_BASE_URL`` default (nginx
    strips it before the backend sees the path). It is written into stored chunk text,
    so a deployment that serves the API elsewhere overrides ``KB_ASSET_URL_PREFIX``
    before indexing rather than rewriting rows afterwards.
    """
    prefix = (os.getenv("KB_ASSET_URL_PREFIX", "/api") or "").rstrip("/")
    return f"{prefix}/v1/catalog/kb/assets/{asset_id}"


# ── Persistence ────────────────────────────────────────────────────────────────


def _is_worth_indexing(asset: RawAsset) -> bool:
    """Filter out layout noise (rules, bullets, logos) before it costs a vision call."""
    if len(asset.data) < _MIN_ASSET_BYTES:
        return False
    try:
        import io

        from PIL import Image  # optional dependency (transitive via python-pptx)

        with Image.open(io.BytesIO(asset.data)) as im:
            width, height = im.size
        if min(width, height) < _MIN_ASSET_EDGE_PX:
            return False
    except Exception:
        # Pillow missing or the bytes are not a decodable image — the byte-size floor
        # above is the fallback filter; never drop an asset because probing failed.
        pass
    return True


def _locate_parent_chunk(parents: Sequence, placeholder: str) -> Optional[object]:
    """Find the parent chunk whose text references this asset."""
    for parent in parents:
        if placeholder and placeholder in (parent.content or ""):
            return parent
    return None


def _rewrite_placeholder(parents: Sequence, placeholder: str, url: str) -> None:
    """Point the markdown image link at the stored asset.

    Before this, ``![](images/<sha>.jpg)`` in a chunk was a dead link — the layout
    service's temp path, with the bytes discarded. Rewriting in both the parent text
    and its children keeps the stored chunk, the embedded text, and what the UI renders
    consistent.
    """
    needle = f"]({placeholder})"
    replacement = f"]({url})"
    for parent in parents:
        if needle in (parent.content or ""):
            parent.content = parent.content.replace(needle, replacement)
        for child in getattr(parent, "children", []) or []:
            if needle in (child.content or ""):
                child.content = child.content.replace(needle, replacement)


def persist_assets(
    db,
    *,
    kb_id: str,
    document_id: str,
    user_id: str,
    assets: Sequence[RawAsset],
    parents: Sequence,
) -> list:
    """Store asset bytes, create ``kb_assets`` rows, and link them to parent chunks.

    Must run **before** the parent chunks are embedded and written: it rewrites the
    placeholder links inside ``parents`` in place, and the rewritten text is what gets
    stored in PostgreSQL and vectorised.

    Failures are per-asset — a storage error on one figure must not fail the document,
    whose text is already parsed and perfectly usable.
    """
    from core.db.models import KBAsset
    from core.storage import generate_storage_key, get_storage

    if not assets:
        return []

    env = os.getenv("ENVIRONMENT", "dev")
    storage = get_storage()
    created: list = []

    for asset in assets:
        if not _is_worth_indexing(asset):
            continue

        asset_id = uuid.uuid4().hex[:32]
        suffix = image_extension_for_mime(asset.mime_type) if asset.kind == "image" else ".bin"
        storage_key = generate_storage_key(
            env=env,
            user_id=user_id,
            category="kb_assets",
            filename=f"{asset_id}{suffix}",
        )
        try:
            storage.upload_bytes(asset.data, storage_key)
        except Exception as exc:
            logger.warning("资产 %s 落盘失败，跳过: %s", asset.placeholder, exc)
            continue

        parent = _locate_parent_chunk(parents, asset.placeholder)
        row = KBAsset(
            asset_id=asset_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_id=getattr(parent, "parent_id", None),
            kind=asset.kind,
            mime_type=asset.mime_type,
            storage_key=storage_key,
            size_bytes=len(asset.data),
            asset_index=asset.index,
            locator=asset.locator or {},
            text_content=asset.text_content or "",
            caption_status="pending",
            vector_state={},
            extra_data={"placeholder": asset.placeholder},
        )
        db.add(row)
        created.append(row)
        _rewrite_placeholder(parents, asset.placeholder, asset_public_url(asset_id))

    if created:
        db.commit()
        for row in created:
            db.refresh(row)
        logger.info("文档 %s 抽出并落盘 %d 个资产", document_id, len(created))
    return created


def purge_document_assets(db, document_id: str) -> int:
    """Delete a document's assets (storage objects + rows). Used on re-index."""
    from core.db.models import KBAsset

    rows = db.query(KBAsset).filter(KBAsset.document_id == document_id).all()
    if not rows:
        return 0

    try:
        from core.storage import get_storage

        storage = get_storage()
        for row in rows:
            try:
                storage.delete(row.storage_key)
            except Exception as exc:
                logger.warning("资产 %s 存储清理失败: %s", row.asset_id, exc)
    except Exception as exc:
        logger.warning("存储后端不可用，仅清理资产表: %s", exc)

    count = db.query(KBAsset).filter(KBAsset.document_id == document_id).delete()
    db.commit()
    return count


# ── 图生文：走视觉桥 ───────────────────────────────────────────────────────────

# 单批提交给视觉桥的图片数。视觉桥的并发信号量是**进程级**的
# （``VISION_MAX_CONCURRENCY``，默认 4），一篇上百张图的文档如果一次性 gather 过去，
# 会把信号量占满，对话里的看图请求全被排在后面——批量离线任务不该抢交互路径的道。
# 按小批提交保证 KB 任何时刻最多占用其中几格，其余留给对话。
_CAPTION_BATCH = max(1, int(os.getenv("KB_ASSET_CAPTION_BATCH", "2")))
# 落进 ``text_content`` 的转写上限：它要参与向量化，一张满是文字的扫描件能顶出几万字。
_MAX_TEXT_CONTENT_CHARS = max(200, int(os.getenv("KB_ASSET_TEXT_MAX_CHARS", "4000")))

# 给视觉桥的 focus：同一套证据契约，但把注意力挪到"文档插图"这个场景上。
_CAPTION_FOCUS = (
    "这是文档中的插图，识别结果会被向量化用于知识库检索。请重点覆盖："
    "图的类型（照片/流程图/架构图/折线图/柱状图/表格截图等）、"
    "图中出现的全部文字与数字（坐标轴、图例、标签、单位）、"
    "以及图所反映的关键结论或趋势。读不出来的一律写进 uncertainty，不要猜。"
)


def vision_available() -> bool:
    """当前是否有可用的图像理解模型（视觉桥）。"""
    try:
        from core.vision import is_available

        return is_available()
    except Exception as exc:  # noqa: BLE001 — 视觉桥不可用不该让索引失败
        logger.debug("视觉桥可用性判定失败，按不可用处理: %s", exc)
        return False


def _describe_batch(images: list, loop=None) -> list:
    """把一批图交给视觉桥识别。返回与输入等长的结果列表（失败位为 None）。

    视觉桥是异步的，而索引是同步函数、跑在没有运行中事件循环的工作线程里。按可靠性
    从高到低三级取宿主循环——只有跑在**宿主循环**上，视觉桥的证据缓存与并发信号量才
    真的生效：

    1. ``loop``：常驻索引 worker 显式传进来的调度循环。这是当前的生产路径——索引跑在
       worker 自己的 ``ThreadPoolExecutor`` 里（见 ``core/kb/index_worker.py``），那不是
       anyio 的工作线程，第 2 级在这里拿不到东西。
    2. ``anyio.from_thread``：仍经 ``BackgroundTasks`` 进来的调用方（如我的空间同步产物
       入库）跑在 Starlette 的 anyio 线程池里，天然带着回宿主循环的通路。
    3. 临时循环 + **关掉证据缓存**：CLI / 脚本 / 测试。``core/infra/redis.py`` 的连接池是
       模块级全局、绑定首次使用它的循环，让一个转瞬即逝的临时循环抢先创建它，会把整个
       进程的 Redis 绑死在已销毁的循环上——那比"这次没吃到缓存"严重得多。
    """
    import asyncio
    import functools

    from core.vision import get_vision_bridge

    bridge = get_vision_bridge()

    if loop is not None and not loop.is_closed():
        future = asyncio.run_coroutine_threadsafe(
            bridge.describe_many(images, focus=_CAPTION_FOCUS), loop
        )
        return future.result()

    try:
        import anyio.from_thread

        return anyio.from_thread.run(
            functools.partial(bridge.describe_many, images, focus=_CAPTION_FOCUS)
        )
    except RuntimeError as exc:
        logger.debug("视觉桥回宿主循环不可用，改用临时循环且不走缓存: %s", exc)

    async def _uncached() -> list:
        tasks = [
            bridge.describe(data, mime, focus=_CAPTION_FOCUS, use_cache=False)
            for data, mime in images
        ]
        return list(await asyncio.gather(*tasks))

    return asyncio.run(_uncached())


def _merge_text_content(existing: str, ocr_text: str) -> str:
    """图注 + OCR 转写合成检索用文本，去重后截断。

    两者都留：图注是文档作者写的、往往点明了图的用途；OCR 是图里实际的字。
    """
    parts: list[str] = []
    for part in ((existing or "").strip(), (ocr_text or "").strip()):
        if part and part not in parts:
            parts.append(part)
    merged = "\n".join(parts)
    return merged[:_MAX_TEXT_CONTENT_CHARS]


def _evidence_digest(result) -> dict:
    """留档进 ``extra_data`` 的证据摘要。

    刻意不存 ``layout.regions`` 与 ``ocr.lines``：整段文字已经进了 ``text_content``，
    再存一份逐行结构，一篇几百张图的文档会把 JSONB 撑得很难看。留下的是之后真会用到
    的部分——实体/关系（可喂本体与 Wiki 层）、不确定项（人工复核时看）、模型元数据
    （换模型后判断哪些资产该重跑）。
    """
    evidence = result.evidence
    return {
        "summary": evidence.summary,
        "scene": evidence.semantics.scene,
        "intent": evidence.semantics.intent,
        "entities": [e.model_dump(exclude_none=True) for e in evidence.semantics.entities],
        "relations": [r.model_dump(exclude_none=True) for r in evidence.semantics.relations],
        "uncertainty": list(evidence.uncertainty),
        "meta": result.to_meta(),
    }


def generate_captions(db, assets: Sequence, *, loop=None) -> int:
    """给资产补上图像理解结果：``caption`` 写概述，``text_content`` 并入 OCR 转写。

    走的是 ``core/vision`` 视觉桥——和对话里"纯文本主模型读图"完全同一条通路、同一个
    ``vision`` 模型角色，因此知识库这边白得三样东西：按图片内容哈希的证据缓存（重新
    索引同一篇文档不用再付一次钱）、三协议与结构化输出降级、以及"该角色只能指派多模态
    模型"的硬约束。

    没有可用的视觉模型是正常状态，不是错误：资产保留文档自带的图注并照常入索引，之后
    配好 ``vision`` 角色再回填即可，**不用重新解析文档**。
    """
    if not assets:
        return 0

    if not vision_available():
        logger.info("视觉桥不可用（未指派 vision 角色且主模型不支持读图），跳过图生文；图注仍会入索引")
        for row in assets:
            row.caption_status = "skipped"
        db.commit()
        return 0

    from core.storage import get_storage

    storage = get_storage()
    images = [a for a in assets if a.kind == "image"]
    budget, overflow = images[:_MAX_CAPTIONS_PER_DOC], images[_MAX_CAPTIONS_PER_DOC:]
    # 没轮到的显式标注，让"上限生效"在数据里看得见，而不是像视觉模型什么都没返回。
    for row in overflow + [a for a in assets if a.kind != "image"]:
        row.caption_status = "skipped"
    if overflow:
        logger.warning(
            "文档图片数 %d 超过单文档图生文上限 %d，超出部分仅用图注索引",
            len(images),
            _MAX_CAPTIONS_PER_DOC,
        )

    done = 0
    for offset in range(0, len(budget), _CAPTION_BATCH):
        chunk = budget[offset : offset + _CAPTION_BATCH]
        payloads: list = []
        pending: list = []
        for row in chunk:
            try:
                data = storage.download_bytes(row.storage_key)
            except Exception as exc:  # noqa: BLE001 — 单张读不出来不该拖垮整篇
                logger.warning("资产 %s 读取失败，跳过图生文: %s", row.asset_id, exc)
                row.caption_status = "failed"
                continue
            payloads.append((data, row.mime_type))
            pending.append(row)

        if not payloads:
            continue

        try:
            results = _describe_batch(payloads, loop)
        except Exception as exc:  # noqa: BLE001 — 整批失败也只降级，正文索引已经好了
            logger.warning("视觉桥调用失败，该批 %d 张仅用图注索引: %s", len(pending), exc)
            for row in pending:
                row.caption_status = "failed"
            continue

        for row, result in zip(pending, results):
            if result is None:
                row.caption_status = "failed"
                continue
            evidence = result.evidence
            summary = (evidence.summary or "").strip()
            row.caption = summary
            row.text_content = _merge_text_content(row.text_content, evidence.ocr.full_text)
            meta = dict(row.extra_data or {})
            meta["vision"] = _evidence_digest(result)
            row.extra_data = meta
            row.caption_status = "completed" if summary else "failed"
            if summary:
                done += 1

    # 这里给 ``extra_data`` 赋的是**新字典**（不是原地改键），SQLAlchemy 的属性事件能
    # 直接捕获，不需要 flag_modified —— 那个只有原地改 JSON 才必须。
    db.commit()
    logger.info("图生文完成 %d/%d", done, len(budget))
    return done


# ── Vector rows ────────────────────────────────────────────────────────────────


def asset_index_text(asset, *, title: str = "") -> str:
    """The text an asset is retrieved by: 视觉桥概述 + 图注/OCR 转写 + 文档标题。

    Audio/video reuse this unchanged — ``text_content`` becomes the ASR transcript.
    """
    parts = [
        (asset.caption or "").strip(),
        (asset.text_content or "").strip(),
        (title or "").strip(),
    ]
    return "\n".join(p for p in parts if p)


def index_assets(
    assets: Sequence, *, user_id: str, kb_id: str, document_id: str, title: str
) -> int:
    """Write one ``row_type="image"`` row per asset into the existing collection.

    ``parent_chunk_id`` points at the chunk that contains the asset, so a hit here
    resolves to that chunk's full text through the existing parent-fetch path. Assets
    with no owning chunk (a standalone upload whose placeholder was lost) point at
    themselves and fall back to the caption as their content.
    """
    if not assets:
        return 0

    from core.kb.kb_vector import (
        TAGS_FIELD_MAX_BYTES,
        TITLE_FIELD_MAX_BYTES,
        embed_batch,
        text_to_sparse,
        truncate_utf8,
        upsert_rows,
    )

    indexable = [(a, asset_index_text(a, title=title)) for a in assets]
    indexable = [(a, t) for a, t in indexable if t.strip()]
    if not indexable:
        return 0

    try:
        vectors = embed_batch([t for _, t in indexable])
    except Exception as exc:
        logger.error("资产向量化失败，文档 %s 的图片不可检索: %s", document_id, exc)
        return 0

    rows = []
    for (asset, text), vector in zip(indexable, vectors):
        rows.append(
            {
                "chunk_id": f"{_ASSET_ROW_PREFIX}{asset.asset_id}",
                "parent_chunk_id": asset.chunk_id or f"{_ASSET_ROW_PREFIX}{asset.asset_id}",
                "row_type": ROW_TYPE_IMAGE,
                "user_id": user_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "title": truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
                "content": truncate_utf8(text),
                "tags_text": truncate_utf8(asset.text_content or "", TAGS_FIELD_MAX_BYTES),
                "chunk_index": asset.asset_index or 0,
                "dense_embedding": vector,
                "sparse_embedding": text_to_sparse(text),
            }
        )

    BATCH = 100
    for i in range(0, len(rows), BATCH):
        upsert_rows(rows[i : i + BATCH])
    logger.info("文档 %s 写入 %d 条资产检索行", document_id, len(rows))
    return len(rows)


def mark_vector_state(db, assets: Sequence, space: str, state: str) -> None:
    """Record that these assets are indexed into a vector space.

    ``space`` is ``"text"`` for the caption/OCR rows written by :func:`index_assets`.
    路径 B 落地时用 ``"visual"``，与文本行互不覆盖。
    """
    for row in assets:
        current = dict(row.vector_state or {})
        current[space] = state
        # 同上：赋新字典而不是原地改键，属性事件自然捕获得到。
        row.vector_state = current
    db.commit()


# ── Retrieval helper ───────────────────────────────────────────────────────────


def fetch_assets_for_chunks(chunk_ids: Iterable[str], *, max_per_chunk: int = 4) -> dict:
    """Map parent-chunk id → its assets, for attaching to retrieval results.

    Also keyed by the synthetic ``a_<asset_id>`` parent used when an asset has no
    owning chunk, so a hit on such a row still resolves to its own image.
    """
    ids = [c for c in dict.fromkeys(chunk_ids) if c]
    if not ids:
        return {}

    direct = [c for c in ids if not c.startswith(_ASSET_ROW_PREFIX)]
    orphan_asset_ids = [c[len(_ASSET_ROW_PREFIX) :] for c in ids if c.startswith(_ASSET_ROW_PREFIX)]

    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBAsset
        from sqlalchemy import or_

        with SessionLocal() as db:
            clauses = []
            if direct:
                clauses.append(KBAsset.chunk_id.in_(direct))
            if orphan_asset_ids:
                clauses.append(KBAsset.asset_id.in_(orphan_asset_ids))
            if not clauses:
                return {}
            rows = db.query(KBAsset).filter(or_(*clauses)).order_by(KBAsset.asset_index).all()
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                key = row.chunk_id or f"{_ASSET_ROW_PREFIX}{row.asset_id}"
                bucket = grouped.setdefault(key, [])
                if len(bucket) >= max_per_chunk:
                    continue
                bucket.append(
                    {
                        "asset_id": row.asset_id,
                        "kind": row.kind,
                        "url": asset_public_url(row.asset_id),
                        "caption": row.caption or row.text_content or "",
                        "locator": row.locator or {},
                    }
                )
            return grouped
    except Exception as exc:
        logger.warning("检索结果附图失败（不影响文本结果）: %s", exc)
        return {}

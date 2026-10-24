"""asset captions responsibilities."""
from __future__ import annotations
from typing import Sequence
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_assets_state

def vision_available() -> bool:
    """当前是否有可用的图像理解模型（视觉桥）。"""
    try:
        from core.models.vision import is_available

        return is_available()
    except Exception as exc:  # noqa: BLE001 — 视觉桥不可用不该让索引失败
        logger.debug("视觉桥可用性判定失败，按不可用处理: %s", exc)
        return False


def describe_batch(images: list, loop=None) -> list:
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

    from core.models.vision import get_vision_bridge

    bridge = get_vision_bridge()

    if loop is not None and not loop.is_closed():
        future = asyncio.run_coroutine_threadsafe(
            bridge.describe_many(images, focus=kb_assets_state.CAPTION_FOCUS), loop
        )
        return future.result()

    try:
        import anyio.from_thread

        return anyio.from_thread.run(
            functools.partial(bridge.describe_many, images, focus=kb_assets_state.CAPTION_FOCUS)
        )
    except RuntimeError as exc:
        logger.debug("视觉桥回宿主循环不可用，改用临时循环且不走缓存: %s", exc)

    async def _uncached() -> list:
        tasks = [
            bridge.describe(data, mime, focus=kb_assets_state.CAPTION_FOCUS, use_cache=False)
            for data, mime in images
        ]
        return list(await asyncio.gather(*tasks))

    return asyncio.run(_uncached())


def merge_text_content(existing: str, ocr_text: str) -> str:
    """图注 + OCR 转写合成检索用文本，去重后截断。

    两者都留：图注是文档作者写的、往往点明了图的用途；OCR 是图里实际的字。
    """
    parts: list[str] = []
    for part in ((existing or "").strip(), (ocr_text or "").strip()):
        if part and part not in parts:
            parts.append(part)
    merged = "\n".join(parts)
    return merged[:kb_assets_state.MAX_TEXT_CONTENT_CHARS]


def evidence_digest(result) -> dict:
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

    from core.files.storage import get_storage

    storage = get_storage()
    images = [a for a in assets if a.kind == "image"]
    budget, overflow = images[:kb_assets_state.MAX_CAPTIONS_PER_DOC], images[kb_assets_state.MAX_CAPTIONS_PER_DOC:]
    # 没轮到的显式标注，让"上限生效"在数据里看得见，而不是像视觉模型什么都没返回。
    for row in overflow + [a for a in assets if a.kind != "image"]:
        row.caption_status = "skipped"
    if overflow:
        logger.warning(
            "文档图片数 %d 超过单文档图生文上限 %d，超出部分仅用图注索引",
            len(images),
            kb_assets_state.MAX_CAPTIONS_PER_DOC,
        )

    done = 0
    for offset in range(0, len(budget), kb_assets_state.CAPTION_BATCH):
        chunk = budget[offset : offset + kb_assets_state.CAPTION_BATCH]
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
            results = describe_batch(payloads, loop)
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
            row.text_content = merge_text_content(row.text_content, evidence.ocr.full_text)
            meta = dict(row.extra_data or {})
            meta["vision"] = evidence_digest(result)
            row.extra_data = meta
            row.caption_status = "completed" if summary else "failed"
            if summary:
                done += 1

    # 这里给 ``extra_data`` 赋的是**新字典**（不是原地改键），SQLAlchemy 的属性事件能
    # 直接捕获，不需要 flag_modified —— 那个只有原地改 JSON 才必须。
    db.commit()
    logger.info("图生文完成 %d/%d", done, len(budget))
    return done

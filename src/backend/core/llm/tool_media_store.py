"""工具返回的媒体在历史里的存放处：内容寻址，按引用落库，装配上下文时取回原件。

``model_steps`` 是「模型自己的历史」的唯一真源——回放它应当得到模型当时真正看过
的那串消息。媒体曾经是这条真源上唯一的缺口：落库时图被换成一行
``[image omitted from replay history: …]``，于是**图只能活一轮**，下一轮模型看到
的是一句「这里原本有张图」。它不报错，模型会把「没看到」当成「看到了但页面是空
的」，比直接失败更难排查。

字节落在 :class:`core.db.models.ToolMediaBlob`，主键就是内容的 sha256；历史里只留
一条引用：

    {"type": "data",
     "source": {"type": "stored", "media_type": "image/png", "sha256": "..."},
     "name": "/workspace/page-3.png"}

**引用要一路留到最后一刻。** 历史被重放、被 JSON 化写进 ``chat_runs.recovery_snapshot``、
被压缩检查点带着走；这些地方要的是「哪张图」而不是像素。所以还原只发生在
:func:`hydrate_rows`——历史行即将变成模型消息的那一步，而且**一次查询取齐**整段
历史用到的所有媒体，而不是每个块查一次。

**为什么不直接内联进 ``model_steps``**：拼上下文要把检查点之后每一条消息的那一列整份
拉出来，一页 200dpi 的图编码后一两兆，内联就意味着每一轮都要整份读一遍，哪怕预算马上
要把那一步挤掉。单开一张表既不撑大历史列，又因为主键是内容哈希而天然去重——同一张图
读多少次都只存一份。

**为什么不放存储后端**：它和历史行属于同一份记录，同库才不会出现「引用还在、字节没了」。
代价是数据库体积和备份重量，这是明知并接受的。

取回失败不静默：换成一条明确的「这份媒体已不可用」文本回执，绝不让模型读到一段看起来
像内容的文字还以为自己看过图。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)

STORED_SOURCE_TYPE = "stored"

MEDIA_UNAVAILABLE = (
    "[{media_type} media recorded in this history can no longer be loaded. Do not guess"
    " what it contained; read it again if you still need it.]"
)

# 取回缓存：同一个 worker 连着服务几轮时省掉重复读盘。上限按字节而不是条数——一条
# 5MB 和一条 5KB 不该算同一个代价。故意不大：多 worker 下相邻轮次常落在别的进程，
# 命中率本就有限，而这份缓存是每进程一份，调大等于按 worker 数翻倍占用常驻内存。
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_cache: "OrderedDict[str, str]" = OrderedDict()
_cache_bytes = 0


def _cache_get(digest: str) -> Optional[str]:
    data = _cache.get(digest)
    if data is not None:
        _cache.move_to_end(digest)
    return data


def _cache_put(digest: str, data_b64: str) -> None:
    global _cache_bytes
    if digest in _cache:
        return
    _cache[digest] = data_b64
    _cache_bytes += len(data_b64)
    while _cache_bytes > _CACHE_MAX_BYTES and len(_cache) > 1:
        _, evicted = _cache.popitem(last=False)
        _cache_bytes -= len(evicted)


def _data_block(block: Mapping[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "data", "source": source}
    if block.get("name"):
        out["name"] = block["name"]
    return out


def is_stored(block: Any) -> bool:
    if not isinstance(block, Mapping):
        return False
    source = block.get("source")
    return isinstance(source, Mapping) and source.get("type") == STORED_SOURCE_TYPE


def media_type_of(block: Mapping[str, Any]) -> str:
    source = block.get("source")
    if isinstance(source, Mapping):
        return str(source.get("media_type") or "unknown")
    return "unknown"


def unavailable_note(block: Mapping[str, Any]) -> Dict[str, Any]:
    """替代取不回的那份媒体交给模型的说明。它是失败回执，不是内容。"""
    return {"type": "text", "text": MEDIA_UNAVAILABLE.format(media_type=media_type_of(block))}


def store(block: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """base64 媒体块 → 引用块。存不下返回 ``None``（调用方如实记一笔，不假装存了）。"""
    source = block.get("source")
    if not isinstance(source, Mapping):
        return None
    data_b64 = source.get("data")
    media_type = str(source.get("media_type") or "application/octet-stream")
    if not isinstance(data_b64, str) or not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except Exception as exc:  # noqa: BLE001 — 坏 base64 不该炸掉整轮
        logger.error("[tool-media] undecodable base64 media (%s): %s", media_type, exc)
        return None

    digest = hashlib.sha256(raw).hexdigest()
    if _cache_get(digest) is None:
        try:
            _insert(digest, media_type, raw)
        except Exception as exc:  # noqa: BLE001 — 落库故障不该打断正在跑的这一轮
            logger.error("[tool-media] persist failed sha256=%s: %s", digest[:12], exc)
            return None
        _cache_put(digest, data_b64)

    return _data_block(
        block,
        {"type": STORED_SOURCE_TYPE, "media_type": media_type, "sha256": digest},
    )


def _insert(digest: str, media_type: str, raw: bytes) -> None:
    """写入一条，已经在就什么都不做——主键是内容哈希，重复写等于同一份。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from core.db.models import ToolMediaBlob

    values = {
        "sha256": digest,
        "media_type": media_type,
        "data": raw,
        "size_bytes": len(raw),
    }
    with SessionLocal() as session:
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            statement = pg_insert(ToolMediaBlob).values(**values).on_conflict_do_nothing()
        else:
            statement = ToolMediaBlob.__table__.insert().prefix_with("OR IGNORE").values(**values)
        session.execute(statement)
        session.commit()


def _load(digests: Sequence[str]) -> Dict[str, str]:
    """一次取齐这批 sha256 的字节，返回 ``{sha256: base64}``；缺的就不在结果里。"""
    from core.db.models import ToolMediaBlob

    with SessionLocal() as session:
        rows = (
            session.query(ToolMediaBlob.sha256, ToolMediaBlob.data)
            .filter(ToolMediaBlob.sha256.in_(list(digests)))
            .all()
        )
    return {digest: base64.b64encode(bytes(data)).decode("ascii") for digest, data in rows}


def _media_blocks(rows: Iterable[Any]) -> List[Mapping[str, Any]]:
    """历史行里所有的媒体引用块。"""
    found: List[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for block in row.get("content") or []:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            output = block.get("output")
            if isinstance(output, list):
                found.extend(item for item in output if is_stored(item))
    return found


def hydrate_rows(rows: Sequence[Any]) -> List[Any]:
    """把历史行里的媒体引用换回真正的字节，整段历史只查一次库。

    没有媒体的历史原样返回，不产生任何查询，也不复制。
    """
    pending = _media_blocks(rows)
    if not pending:
        return list(rows)

    digests = {str(block["source"]["sha256"]) for block in pending}
    missing = [digest for digest in digests if _cache_get(digest) is None]
    if missing:
        try:
            for digest, data_b64 in _load(missing).items():
                _cache_put(digest, data_b64)
        except Exception as exc:  # noqa: BLE001 — 取不回就如实告诉模型，不抛错
            logger.error("[tool-media] restore failed (%d refs): %s", len(missing), exc)

    def resolved(block: Mapping[str, Any]) -> Dict[str, Any]:
        digest = str(block["source"].get("sha256") or "")
        data_b64 = _cache_get(digest)
        if data_b64 is None:
            logger.error("[tool-media] blob missing sha256=%s", digest[:12])
            return unavailable_note(block)
        return _data_block(
            block,
            {
                "type": "base64",
                "media_type": media_type_of(block),
                "data": data_b64,
            },
        )

    hydrated: List[Any] = []
    for row in rows:
        blocks = row.get("content") if isinstance(row, Mapping) else None
        if not isinstance(blocks, list) or not _media_blocks([row]):
            hydrated.append(row)
            continue
        new_blocks = []
        for block in blocks:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                output = block.get("output")
                if isinstance(output, list):
                    block = {
                        **block,
                        "output": [resolved(item) if is_stored(item) else item for item in output],
                    }
            new_blocks.append(block)
        hydrated.append({**row, "content": new_blocks})
    return hydrated


def reset_cache() -> None:
    """测试用：清空取回缓存。"""
    global _cache_bytes
    _cache.clear()
    _cache_bytes = 0

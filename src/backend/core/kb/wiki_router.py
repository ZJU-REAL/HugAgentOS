"""按知识库 ID 选择 Wiki 实现。

平台里有两类知识库，它们的 Wiki 来源完全不同：

* **自建库**（``kb_`` 前缀）——Wiki 由本地管线生成，存在 ``kb_wiki_pages``；
* **外接库**——Wiki 由外部知识后端提供，我们只做透传。

两侧的实现模块函数面同构，所以调用方只需要在这里选到对的模块，其余代码
（路由、MCP 工具、前端组件）完全不用区分。
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOCAL_KB_PREFIX = "kb_"


def is_local_kb(kb_id: str) -> bool:
    return str(kb_id or "").startswith(LOCAL_KB_PREFIX)


def local_wiki_enabled(kb_id: str) -> bool:
    """自建库是否开启了 Wiki 索引模式。"""
    if not is_local_kb(kb_id):
        return False
    try:
        from core.db.engine import SessionLocal
        from core.kb.wiki.config import wiki_enabled_for_kb

        with SessionLocal() as db:
            return wiki_enabled_for_kb(db, kb_id)
    except Exception as exc:  # noqa: BLE001 - 探测失败一律按未开处理
        logger.warning("检测知识库 %s 的 Wiki 能力失败: %s", kb_id, exc)
        return False


def wiki_module_for(kb_id: str) -> Optional[ModuleType]:
    """取该知识库的 Wiki 实现模块；不支持时返回 None（调用方据此 404）。"""
    if is_local_kb(kb_id):
        if not local_wiki_enabled(kb_id):
            return None
        from core.kb.wiki import local_provider

        return local_provider

    try:
        from core.kb.external_provider import wiki_module

        return wiki_module()
    except Exception as exc:  # noqa: BLE001 - 社区版下外接接缝可能整体缺席
        logger.debug("外接知识库 Wiki 模块不可用: %s", exc)
        return None


def supports_wiki(kb_id: str = "") -> bool:
    """某个库（或整个平台）是否有 Wiki 能力。

    不传 kb_id 时回答的是"平台上是否存在任何 Wiki 源"——用于决定要不要暴露
    Wiki 相关的工具与入口。
    """
    if kb_id:
        return wiki_module_for(kb_id) is not None
    return _external_supports_wiki() or _any_local_wiki_kb()


def _external_supports_wiki() -> bool:
    try:
        from core.kb.external_provider import supports_wiki as external_supports

        return bool(external_supports())
    except Exception:
        return False


def _any_local_wiki_kb() -> bool:
    """库里是否存在任一开启了 Wiki 的自建知识库。

    这个探测跑在 MCP 的 list_tools 上，每次装配会话都会调一次，所以只取 metadata
    一列、命中即停——不能为了一个布尔值把整张 kb_spaces 的 ORM 实例都实例化出来。

    判定依据是**索引模式**而不是「已经生成出页面」：生成是异步的，按页数判定会让
    工具在生成期间忽隐忽现。
    """
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBSpace
        from core.kb.wiki.config import INDEX_MODE_WIKI, normalize_index_modes

        with SessionLocal() as db:
            rows = db.query(KBSpace.extra_data).filter(KBSpace.deleted_at.is_(None)).yield_per(200)
            for (metadata,) in rows:
                if not isinstance(metadata, dict):
                    continue
                if INDEX_MODE_WIKI in normalize_index_modes(metadata.get("index_modes")):
                    return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("探测自建 Wiki 知识库失败: %s", exc)
        return False


def wiki_capable_local_kb_ids(kb_ids: List[str]) -> List[str]:
    """从给定的自建库 ID 里筛出开了 Wiki 的那些，保持原顺序。"""
    candidates = [k for k in kb_ids if is_local_kb(k)]
    if not candidates:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.kb.wiki.config import INDEX_MODE_WIKI, wiki_enabled_kb_ids_map

        with SessionLocal() as db:
            modes = wiki_enabled_kb_ids_map(db, candidates)
    except Exception as exc:  # noqa: BLE001
        logger.warning("筛选 Wiki 知识库失败: %s", exc)
        return []
    return [k for k in candidates if INDEX_MODE_WIKI in modes.get(k, ())]


def capabilities_for_local_kb(space: Any) -> Dict[str, bool]:
    """把自建库的索引模式投影成能力位，供 catalog 与前端消费。"""
    from core.kb.wiki.config import rag_enabled, wiki_enabled

    has_wiki = wiki_enabled(space)
    has_rag = rag_enabled(space)
    return {
        "vector": has_rag,
        "keyword": has_rag,
        "wiki": has_wiki,
        "graph": has_wiki,
    }

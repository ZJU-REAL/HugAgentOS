"""跨会话历史工具：让智能体自己检索并读取用户的其它会话。

模型在一次对话里只看得见当前这段上下文。用户常常在别的会话里已经交代过背景、给过
数据、定过口径，再问一次等于从头来过。这两个工具补上那条路：先用
``list_related_chats`` 找到相关会话，再用 ``read_chat`` 读它的概括或原文。

读取范围由注册时闭包里的 ``user_id`` 锁死，每次访问都走
``ChatService.get_session_with_access``——模型即使编出一个 chat_id 也读不到别人的会话。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.llm.tools._common import resp_json

logger = logging.getLogger(__name__)


def register_chat_history_tools(
    toolkit,
    *,
    user_id: str,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> None:
    """注册 ``list_related_chats`` / ``read_chat``。"""

    if not user_id:
        return

    scope_hint = "当前项目内的会话" if project_id else "你最近的会话"

    def _list(query: str, limit: int):
        from core.db.engine import SessionLocal
        from core.services.chat_reference_service import list_referencable_sessions, session_brief

        with SessionLocal() as db:
            sessions = list_referencable_sessions(
                db,
                user_id,
                project_id=project_id,
                query=query,
                limit=limit,
                exclude_chat_id=chat_id,
            )
            return {
                "ok": True,
                "scope": "project" if project_id else "user",
                "count": len(sessions),
                "chats": [session_brief(session) for session in sessions],
            }

    def _read(target_chat_id: str, mode: str, offset: int, limit: int):
        from core.db.engine import SessionLocal
        from core.services.chat_reference_service import read_session_messages, session_card

        with SessionLocal() as db:
            card = session_card(db, target_chat_id, user_id)
            if card is None:
                return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
            if mode == "card":
                return {"ok": True, "mode": "card", **card}
            page = read_session_messages(db, target_chat_id, user_id, offset=offset, limit=limit)
            if page is None:
                return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
            return {"ok": True, "mode": "full", "title": card["title"], **page}

    async def _digest(target_chat_id: str):
        from core.services.chat_reference_service import build_session_digest

        result = await build_session_digest(target_chat_id, user_id)
        if result is None:
            return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
        return {"ok": True, "mode": "digest", **result}

    async def list_related_chats(query: str = "", limit: int = 20):
        """列出可供参考的历史会话（{scope}），每条给出标题、最后活跃时间和消息条数。

        用户提到"上次""之前那个""我们讨论过的"等指向过往对话时先调本工具定位，
        再用 read_chat 读取那一条的概括或原文。本工具只给标题级信息，不含正文。

        Args:
            query: 可选关键词，按标题和消息正文筛选；留空表示按最近活跃列出。
            limit: 最多返回多少条，默认 20，上限 50。
        """
        try:
            return resp_json(await asyncio.to_thread(_list, query or "", limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[chat-history] list failed: %s", exc, exc_info=True)
            return resp_json({"ok": False, "error": f"检索历史会话失败：{exc}"})

    async def read_chat(
        chat_id: str,
        mode: str = "full",
        offset: int = 0,
        limit: int = 20,
    ):
        """读取一段历史会话的内容。

        Args:
            chat_id: 目标会话 ID，来自 list_related_chats 或用户引用的会话名片。
            mode: full=按页读原文（默认）；digest=生成整段会话的概括，长会话先用它定位再读原文；card=只要名片。
            offset: full 模式下从第几条消息开始，配合返回的 next_offset 翻页。
            limit: full 模式下本次最多读多少条，默认 20，上限 50。

        full 模式单条消息和单次返回都有字数上限，超出会截断并在 has_more / next_offset
        中说明；不要因为一次没读全就断言会话里没有某段内容。
        """
        target = (chat_id or "").strip()
        if not target:
            return resp_json({"ok": False, "error": "chat_id 不能为空"})
        normalized = (mode or "full").strip().lower()
        if normalized not in {"full", "digest", "card"}:
            return resp_json(
                {"ok": False, "error": f"mode 只能是 full / digest / card，收到 {mode}"}
            )
        try:
            if normalized == "digest":
                return resp_json(await _digest(target))
            return resp_json(await asyncio.to_thread(_read, target, normalized, offset, limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[chat-history] read failed: %s", exc, exc_info=True)
            return resp_json({"ok": False, "error": f"读取历史会话失败：{exc}"})

    list_related_chats.__doc__ = (list_related_chats.__doc__ or "").format(scope=scope_hint)

    toolkit.register_tool_function(list_related_chats)
    toolkit.register_tool_function(read_chat)

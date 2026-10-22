"""kb helpers responsibilities."""
from __future__ import annotations
from typing import Optional
from core.infra.exceptions import AccessDeniedError
from core.models.chat_models import get_summarize_model
from core.llm.runtime.message_compat import extract_text_from_chat_response
import logging
logger = logging.getLogger(__name__)

def can_read_space(db, space, user_id: str) -> bool:
    """Read permission: owner / everyone for public KBs / grantees of a scoped-visibility KB (single-source-of-truth resolution)."""
    from features.knowledge.edition import resolve_local_kb_level

    return resolve_local_kb_level(db, user_id, space.kb_id) != "none"


def require_kb_write(db, space, user_id: str, required: str) -> None:
    """Write permission check: required is 'edit' (upload/reindex) or 'admin' (rename/delete/edit chunks).

    Raises AccessDeniedError when insufficient. The owner is always admin, superadmins are always admin, granted users go by their level.
    """
    from features.knowledge.edition import has_kb_permission, resolve_local_kb_level

    level = resolve_local_kb_level(db, user_id, space.kb_id)
    if not has_kb_permission(level, required):
        raise AccessDeniedError(message="Access denied", reason=f"requires kb level >= {required}")


def enqueue_document_indexing(
    db,
    *,
    document_id: str,
    user_id: str,
    chunk_method: str,
    indexing_config: Optional[dict],
    index_modes: Optional[list],
) -> None:
    """把文档排进持久化索引队列，交给常驻 worker 执行。

    索引不能再跑在请求线程池里：那是 Starlette 与所有同步依赖（``get_db``）共用的
    池子，批量上传会把它占满，导致整个后端在 handler 之前就排队。详见
    :mod:`core.kb.index_worker`。
    """
    from features.knowledge.models import KBDocument
    from features.knowledge import index_queue

    doc = db.query(KBDocument).filter(KBDocument.document_id == document_id).first()
    if doc is None:
        logger.warning("Cannot enqueue indexing: document %s not found", document_id)
        return
    index_queue.enqueue(
        db,
        doc,
        user_id=user_id,
        chunk_method=chunk_method,
        indexing_config=indexing_config,
        index_modes=index_modes,
    )


def fallback_kb_description(name: str, description: str) -> str:
    base_name = (name or "").strip()
    base_desc = " ".join((description or "").split())
    if base_desc:
        return (
            f"{base_name}相关资料，适合检索概念说明、政策信息、行业动态、业务规则和常见问题。"
            f"当前范围包括：{base_desc[:120]}"
        )[:220]
    return (
        f"{base_name}相关内容，适合检索概念说明、政策解读、业务规则、行业动态、"
        f"典型案例和常见问题。"
    )[:220]


async def generate_kb_description(name: str, description: str) -> str:
    prompt = f"""/no_think
你是一名知识库配置助手。请根据知识库名称和现有简介，生成一段更利于智能路由判断的“知识库简介”。

目标：
1. 帮助系统快速判断什么问题应该路由到这个知识库
2. 文本要像知识库简介，不是宣传文案
3. 明确主题范围、文档类型、适合回答的问题类型
4. 用中文，1 段即可，控制在 60 到 120 字
5. 不要使用“本知识库可以帮助你”这类口语化表达
6. 不要使用“该知识库聚焦”“本知识库涵盖”这类空泛起手式
7. 直接输出简介正文，不要加标题、引号、序号或解释

知识库名称：{name}
现有简介：{description or "无"}
"""

    try:
        model = get_summarize_model()
        # AgentScope 2.0: model.__call__ takes list[Msg] (not list[dict])
        from agentscope.message import Msg, TextBlock

        result = await model(
            messages=[Msg(name="user", role="user", content=[TextBlock(type="text", text=prompt)])]
        )
        polished = extract_text_from_chat_response(result).strip()
        polished = " ".join(polished.split())
        return polished or fallback_kb_description(name, description)
    except Exception as exc:
        logger.warning("Failed to polish KB description for %s: %s", name, exc)
        return fallback_kb_description(name, description)

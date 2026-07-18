"""Conversation classification API routes."""

from typing import List, Dict
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.auth.backend import get_current_user, UserContext
from core.infra.responses import success_response
from core.llm.classifier import get_classifier, BUSINESS_TOPICS
from core.infra.exceptions import BadRequestError

router = APIRouter(prefix="/v1/classify", tags=["Classify"])


class ClassifyRequest(BaseModel):
    """Request model for conversation classification."""
    messages: List[Dict[str, str]] = Field(
        ...,
        description="List of conversation messages with 'role' and 'content'"
    )


@router.post("", summary="对话业务分类")
async def classify_conversation(
    request: ClassifyRequest,
    user: UserContext = Depends(get_current_user)
):
    """将一段对话分类到业务主题类别。

    类别：综合咨询、政策解读、事项办理、材料比对、知识检索、数据分析。
    入参为待分类的消息列表；功能未启用时回退为「综合咨询」。
    """
    if not request.messages:
        raise BadRequestError(
            message="Messages list cannot be empty",
            data={"provided_messages": len(request.messages)}
        )

    classifier = get_classifier()

    if not classifier.enabled:
        return success_response(
            data={"topic": "综合咨询", "enabled": False},
            message="Classification feature is disabled"
        )

    topic = await classifier.classify_conversation(request.messages)

    if not topic:
        topic = "综合咨询"

    return success_response(
        data={"topic": topic, "enabled": True, "available_topics": BUSINESS_TOPICS},
        message="Classification completed successfully"
    )

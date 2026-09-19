"""Conversation classification service using LLM."""

from __future__ import annotations
import logging
from typing import Dict, List, Optional

from core.config.settings import settings
# 分类与摘要共用 summarizer 角色，所以共用同一个解析器，避免两份会漂移的副本。
from core.llm.summarizer import _resolve_summarizer_endpoint
from core.llm.single_turn import complete

_LOGGER = logging.getLogger(__name__)

BUSINESS_TOPICS = ['综合咨询', '政策解读', '事项办理', '材料比对', '知识检索', '数据分析']


class ConversationClassifier:
    """Classify conversations into business topics using LLM."""

    def __init__(self):
        self.enabled = settings.llm.enable_summary
        self.max_rounds = settings.llm.summary_max_rounds

        if not self.enabled:
            _LOGGER.info("Classification feature disabled via ENABLE_SUMMARY env var")

    def _build_classify_prompt(self, messages: List[Dict[str, str]]) -> str:
        user_messages = [msg["content"] for msg in messages if msg.get("role") == "user"][:self.max_rounds]
        conversation_text = "\n".join(f"用户: {m}" for m in user_messages)
        topics_str = "、".join(BUSINESS_TOPICS)

        return f"""/no_think
请根据以下对话内容，将其分类为最合适的业务主题。

对话内容：
{conversation_text}

可选分类：{topics_str}

要求：
1. 只输出一个分类名称，不要任何解释
2. 必须从可选分类中选择一个
3. 如果无法判断，输出"综合咨询"

分类："""

    async def classify_conversation(
        self,
        messages: List[Dict[str, str]],
        timeout: int = 30,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        endpoint = _resolve_summarizer_endpoint()
        if endpoint is None:
            _LOGGER.debug("Classification skipped: model not configured")
            return None
        if not messages:
            return None

        try:
            result = (
                await complete(
                    endpoint,
                    self._build_classify_prompt(messages),
                    temperature=0.1,
                    max_tokens=2048,
                    timeout=timeout,
                )
            ).text

            for topic in BUSINESS_TOPICS:
                if topic in result:
                    _LOGGER.info("Classified as: %s", topic)
                    return topic

            _LOGGER.warning(
                "LLM returned unexpected classification: %r, defaulting to 综合咨询", result[:100]
            )
            return "综合咨询"

        except Exception as e:
            _LOGGER.error("Failed to classify conversation: %s", e)
            return None


# Singleton instance
_classifier: ConversationClassifier | None = None


def get_classifier() -> ConversationClassifier:
    global _classifier
    if _classifier is None:
        _classifier = ConversationClassifier()
    return _classifier

"""Conversation summarization service using LLM."""

from __future__ import annotations
import logging
from typing import Dict, List, Optional

from core.config.settings import settings
from core.llm.single_turn import Endpoint, complete

_LOGGER = logging.getLogger(__name__)


def _resolve_summarizer_endpoint() -> Optional[Endpoint]:
    """Resolve the summarizer role's endpoint from DB."""
    try:
        from core.services.model_config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("summarizer")
        if cfg and cfg.base_url and cfg.api_key and cfg.model_name:
            return Endpoint.from_resolved(cfg)
    except Exception as exc:
        _LOGGER.debug("ModelConfigService unavailable for summarizer: %s", exc)
    return None


class ConversationSummarizer:
    """Generate concise summaries for conversations using LLM."""

    def __init__(self):
        self.enabled = settings.llm.enable_summary
        self.max_rounds = settings.llm.summary_max_rounds

        if not self.enabled:
            _LOGGER.info("Summary feature disabled via ENABLE_SUMMARY env var")

    def _build_summary_prompt(self, messages: List[Dict[str, str]]) -> str:
        user_messages = [msg["content"] for msg in messages if msg.get("role") == "user"][:self.max_rounds]
        assistant_messages = [msg["content"] for msg in messages if msg.get("role") == "assistant"][:self.max_rounds]

        conversation_text = ""
        for i, (user_msg, assistant_msg) in enumerate(zip(user_messages, assistant_messages), 1):
            conversation_text += f"用户 {i}: {user_msg}\n"
            if assistant_msg:
                preview = assistant_msg[:200] if len(assistant_msg) > 200 else assistant_msg
                conversation_text += f"助手 {i}: {preview}\n"

        if len(user_messages) > len(assistant_messages):
            conversation_text += f"用户 {len(user_messages)}: {user_messages[-1]}\n"

        return f"""/no_think
请为以下对话生成一个简洁的标题摘要（不超过20个字）。

对话内容：
{conversation_text}
要求：
1. 标题要简洁明了，突出对话的核心主题
2. 不超过20个字
3. 使用中文
4. 不要包含"对话"、"聊天"等词
5. 直接输出标题，不要其他解释

标题："""

    async def summarize_conversation(
        self,
        messages: List[Dict[str, str]],
        timeout: int = 30,
    ) -> str | None:
        if not self.enabled:
            return None

        endpoint = _resolve_summarizer_endpoint()
        if endpoint is None:
            _LOGGER.debug("Summarization skipped: model not configured")
            return None
        if not messages:
            return None

        try:
            result = await complete(
                endpoint,
                self._build_summary_prompt(messages),
                temperature=0.3,
                max_tokens=2048,
                timeout=timeout,
            )
            summary = result.text.strip('"\'。！？,.!? \n\t')[:30]
            _LOGGER.info("Generated summary: %s", summary)
            return summary or None

        except Exception as e:
            _LOGGER.error("Failed to generate summary: %s", e)
            return None


# Singleton instance
_summarizer: ConversationSummarizer | None = None


def get_summarizer() -> ConversationSummarizer:
    global _summarizer
    if _summarizer is None:
        _summarizer = ConversationSummarizer()
    return _summarizer

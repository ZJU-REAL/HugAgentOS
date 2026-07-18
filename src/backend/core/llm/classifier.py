"""Conversation classification service using LLM."""

from __future__ import annotations
import logging
from typing import Dict, List, Optional

import httpx

from core.config.settings import settings

_LOGGER = logging.getLogger(__name__)

BUSINESS_TOPICS = ['综合咨询', '政策解读', '事项办理', '材料比对', '知识检索', '数据分析']


def _strip_thinking(text: str) -> str:
    for close_tag in ("</think>", "</thinking>"):
        idx = text.rfind(close_tag)
        if idx != -1:
            return text[idx + len(close_tag):].strip()
    return text.strip()


def _resolve_classifier_config() -> tuple[str, str, str]:
    """Resolve model config from DB (summarizer role, shared with classification)."""
    try:
        from core.services.model_config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("summarizer")
        if cfg:
            return cfg.base_url, cfg.api_key, cfg.model_name
    except Exception as exc:
        _LOGGER.debug("ModelConfigService unavailable for classifier: %s", exc)
    return "", "", ""


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

        model_url, api_key, model_name = _resolve_classifier_config()
        if not model_url or not api_key or not model_name:
            _LOGGER.debug("Classification skipped: model not configured")
            return None
        if not messages:
            return None

        try:
            prompt = self._build_classify_prompt(messages)

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{model_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 2048,
                        "enable_thinking": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )

                if response.status_code != 200:
                    _LOGGER.error(
                        "Classification API error: %s %s", response.status_code, response.text[:200]
                    )
                    return None

                data = response.json()
                raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                result = _strip_thinking(raw)

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

"""Independent follow-up question generator.

After the main response has finished streaming, this module makes a
separate lightweight LLM call to generate 1-3 follow-up questions
the user might want to ask next.  The questions are returned as
structured data (not embedded in the response text), so the frontend
can render them as clickable buttons.

Uses the same httpx + OpenAI-compatible API pattern as summarizer.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

import httpx

from core.config.settings import settings

_LOGGER = logging.getLogger(__name__)

# ── config (read once at import, refreshed via singleton) ──────────────

_PROMPT_TEMPLATE = """/no_think
根据以下用户提问和助手回答，生成1-3个用户可能想继续追问的延伸问题。

用户提问：{user_message}

助手回答（前500字）：{assistant_preview}

要求：问题与对话紧密相关，简短明确（8-40字），以"？"结尾，不重复已问内容。
如果回答已完整则返回空数组。不要输出思考过程，直接返回JSON数组：
["问题1？", "问题2？", "问题3？"]
"""


def _strip_thinking(text: str) -> str:
    """Strip thinking blocks from model output.

    Handles both XML-style (<think>...</think>) and text-style
    (Thinking Process:...) thinking blocks.
    """
    # XML-style: <think>...</think> or <thinking>...</thinking>
    for close_tag in ("</think>", "</thinking>"):
        idx = text.rfind(close_tag)
        if idx != -1:
            return text[idx + len(close_tag) :].strip()

    # Text-style: find the JSON array after thinking text
    # Look for the first '[' that starts a JSON array
    bracket_idx = text.find("[")
    if bracket_idx > 0:
        # Only strip if there's substantial text before the bracket
        # (indicating thinking output before the actual answer)
        prefix = text[:bracket_idx].strip()
        if len(prefix) > 20:
            return text[bracket_idx:].strip()

    return text.strip()


def _resolve_followup_config() -> tuple[str, str, str]:
    """Resolve model config from DB: try 'followup' role, then 'summarizer', then 'main_agent'."""
    try:
        from core.services.model_config import ModelConfigService
        svc = ModelConfigService.get_instance()
        for role in ("followup", "summarizer", "main_agent"):
            cfg = svc.resolve(role)
            if cfg:
                return cfg.base_url, cfg.api_key, cfg.model_name
    except Exception as exc:
        _LOGGER.debug("ModelConfigService unavailable for followup: %s", exc)
    return "", "", ""


class FollowUpGenerator:
    """Generate follow-up questions via a separate LLM call."""

    def __init__(self) -> None:
        self.enabled: bool = settings.routing.followup_enabled

        if not self.enabled:
            _LOGGER.info("Follow-up question generation disabled via FOLLOWUP_ENABLED")

    async def generate(
        self,
        user_message: str,
        assistant_response: str,
        timeout: int = 10,
    ) -> List[str]:
        """Return 0-3 follow-up question strings.

        Never raises – returns an empty list on any error.
        """
        if not self.enabled:
            _LOGGER.warning("[followup] disabled, skipping")
            return []
        model_url, api_key, model_name = _resolve_followup_config()
        if not model_url or not api_key or not model_name:
            _LOGGER.warning("[followup] no model config resolved (url=%s, model=%s)", bool(model_url), model_name)
            return []
        if not user_message or not assistant_response:
            _LOGGER.warning("[followup] empty input: user_msg=%d, assistant=%d", len(user_message or ""), len(assistant_response or ""))
            return []
        # Skip very short responses (greetings, errors, etc.)
        if len(assistant_response.strip()) < 40:
            _LOGGER.warning("[followup] response too short (%d chars), skipping", len(assistant_response.strip()))
            return []

        _LOGGER.info("[followup] generating for response (%d chars), model=%s", len(assistant_response), model_name)

        try:
            prompt = _PROMPT_TEMPLATE.format(
                user_message=user_message[:300],
                assistant_preview=assistant_response[:500],
            )

            # Build request body — avoid sending thinking-related params
            # that may cause errors on models that don't support them.
            req_body: dict = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.5,
                "max_tokens": 512,
            }
            # For thinking-capable models, explicitly disable thinking
            model_lower = model_name.lower()
            if any(k in model_lower for k in ("deepseek", "r1", "qwen")):
                req_body["chat_template_kwargs"] = {"enable_thinking": False}

            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{model_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=req_body,
                )

            if resp.status_code != 200:
                _LOGGER.warning(
                    "[followup] API error: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []

            raw = (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            _LOGGER.info("[followup] raw LLM response (%d chars): %s", len(raw), raw[:200])
            raw = _strip_thinking(raw)
            questions = _parse_questions(raw)
            _LOGGER.info("[followup] parsed %d questions: %s", len(questions), questions)
            return questions

        except Exception as exc:
            _LOGGER.warning("[followup] generation failed: %r", exc, exc_info=True)
            return []


def _parse_questions(raw: str) -> List[str]:
    """Parse the LLM's JSON array output into a clean list of questions."""
    raw = raw.strip()

    # Try direct JSON parse
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return _clean_list(arr)
    except json.JSONDecodeError:
        pass

    # Fallback: extract JSON array from surrounding text
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            arr = json.loads(match.group())
            if isinstance(arr, list):
                return _clean_list(arr)
        except json.JSONDecodeError:
            pass

    return []


def _clean_list(items: list) -> List[str]:
    """Validate and clean a list of question strings."""
    result: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        q = item.strip().strip("\"' \t")
        # Remove markdown bold
        q = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", q)
        if len(q) < 4 or len(q) > 80:
            continue
        result.append(q)
        if len(result) >= 3:
            break
    return result


# ── Singleton ──────────────────────────────────────────────────────────

_instance: Optional[FollowUpGenerator] = None


def get_followup_generator() -> FollowUpGenerator:
    global _instance
    if _instance is None:
        _instance = FollowUpGenerator()
    return _instance

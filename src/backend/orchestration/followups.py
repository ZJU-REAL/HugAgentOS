"""Independent follow-up question generator.

After the main response has finished streaming, this module makes a
separate lightweight LLM call to generate 1-3 follow-up questions
the user might want to ask next.  The questions are returned as
structured data (not embedded in the response text), so the frontend
can render them as clickable buttons.

走 core.llm.single_turn，端点用哪条协议由它按配置决定。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

from core.config.settings import settings
from core.llm.single_turn import Endpoint, complete

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


def _resolve_followup_endpoint() -> Optional[Endpoint]:
    """Resolve model config from DB: try 'followup' role, then 'summarizer', then 'main_agent'."""
    try:
        from core.services.model_config import ModelConfigService

        svc = ModelConfigService.get_instance()
        for role in ("followup", "summarizer", "main_agent"):
            cfg = svc.resolve(role)
            if cfg and cfg.base_url and cfg.api_key and cfg.model_name:
                return Endpoint.from_resolved(cfg)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("ModelConfigService unavailable for followup: %s", exc)
    return None


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
        run_id: str = "",
        usage_recorder: Any = None,
    ) -> list[str]:
        """Return 0-3 follow-up question strings.

        Never raises – returns an empty list on any error.
        """
        if not self.enabled:
            _LOGGER.warning("[followup] disabled, skipping")
            return []
        endpoint = _resolve_followup_endpoint()
        if endpoint is None:
            _LOGGER.warning("[followup] no model config resolved")
            return []
        model_name = endpoint.model_name
        if not user_message or not assistant_response:
            _LOGGER.warning(
                "[followup] empty input: user_msg=%d, assistant=%d",
                len(user_message or ""),
                len(assistant_response or ""),
            )
            return []
        # Skip very short responses (greetings, errors, etc.)
        if len(assistant_response.strip()) < 40:
            _LOGGER.warning(
                "[followup] response too short (%d chars), skipping",
                len(assistant_response.strip()),
            )
            return []

        _LOGGER.info(
            "[followup] generating for response (%d chars), model=%s",
            len(assistant_response),
            model_name,
        )

        started = time.monotonic()
        status = "failed"
        response_usage: dict = {}
        try:
            prompt = _PROMPT_TEMPLATE.format(
                user_message=user_message[:300],
                assistant_preview=assistant_response[:500],
            )

            result = await complete(
                endpoint,
                prompt,
                temperature=0.5,
                max_tokens=512,
                timeout=timeout,
            )

            status = "success"
            response_usage = result.usage
            raw = result.text
            _LOGGER.info(
                "[followup] raw LLM response (%d chars): %s", len(raw), raw[:200]
            )
            questions = _parse_questions(raw)
            _LOGGER.info(
                "[followup] parsed %d questions: %s", len(questions), questions
            )
            return questions

        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            from core.harness.usage import attempt_status_for_exception

            status = attempt_status_for_exception(exc)
            _LOGGER.warning("[followup] generation failed: %r", exc, exc_info=True)
            return []
        finally:
            if run_id:
                try:
                    from core.harness.usage import (
                        AttemptUsage,
                        UsageAttempt,
                        record_usage_safely,
                    )
                    from core.services.harness_ledger import HarnessUsageLedger

                    recorder = usage_recorder or HarnessUsageLedger()
                    await record_usage_safely(
                        recorder,
                        UsageAttempt(
                            run_id=run_id,
                            kind="model",
                            operation_name=model_name,
                            provider=endpoint.provider,
                            model=model_name,
                            status=status,
                            latency_ms=int((time.monotonic() - started) * 1_000),
                            usage=AttemptUsage(
                                prompt_tokens=int(
                                    response_usage.get("prompt_tokens") or 0
                                ),
                                completion_tokens=int(
                                    response_usage.get("completion_tokens") or 0
                                ),
                                cache_read_tokens=int(
                                    response_usage.get("cache_read_tokens") or 0
                                ),
                                cache_write_tokens=int(
                                    response_usage.get("cache_write_tokens") or 0
                                ),
                            ),
                            metadata={"source": "followup"},
                        ),
                    )
                except Exception:
                    _LOGGER.debug("[followup] usage persistence failed", exc_info=True)


def _parse_questions(raw: str) -> list[str]:
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


def _clean_list(items: list) -> list[str]:
    """Validate and clean a list of question strings."""
    result: list[str] = []
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

_instance: FollowUpGenerator | None = None


def get_followup_generator() -> FollowUpGenerator:
    global _instance
    if _instance is None:
        _instance = FollowUpGenerator()
    return _instance

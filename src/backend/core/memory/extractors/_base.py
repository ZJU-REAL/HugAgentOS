"""Extractor base: shared LLM invocation, JSON parsing, timeout protection.

Each concrete extractor only needs to provide:
- A PROMPT template (with {user_msg} {assistant_msg} {curr_date} placeholders)
- Result field parsing (optional)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from typing import Any, Optional

from core.config.settings import settings

logger = logging.getLogger(__name__)


# Lazily-loaded OpenAI client + resolved model name (avoids errors at import time)
_client = None
_model_name: Optional[str] = None
_client_lock = asyncio.Lock()


def _resolve_memory_model_config() -> tuple[str, str, str]:
    """Returns (base_url, api_key, model_name).

    Priority: the DB `memory` role config (via ModelConfigService) → env fallback.
    Consistent with `core/memory/service.py::_build_mem0_config`.
    """
    try:
        from core.services.model_config import ModelConfigService
        svc = ModelConfigService.get_instance()
        mem_cfg = svc.resolve("memory")
        if mem_cfg and mem_cfg.base_url and mem_cfg.api_key:
            return mem_cfg.base_url, mem_cfg.api_key, mem_cfg.model_name
    except Exception as exc:
        logger.debug("[extractor] DB memory config unavailable: %s", exc)

    cfg = settings.memory
    return cfg.model_url or "", cfg.api_key or "", cfg.model_name or ""


async def _get_client() -> tuple[object, str]:
    """A dedicated memory LLM client, not shared with the main conversation. Returns (client, model_name)."""
    global _client, _model_name
    if _client is not None and _model_name is not None:
        return _client, _model_name
    async with _client_lock:
        if _client is not None and _model_name is not None:
            return _client, _model_name
        from openai import AsyncOpenAI
        base_url, api_key, model_name = _resolve_memory_model_config()
        if not base_url or not api_key or not model_name:
            raise RuntimeError(
                f"memory LLM config incomplete: base_url={bool(base_url)} "
                f"api_key={bool(api_key)} model_name={bool(model_name)}"
            )
        _client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=30.0,
        )
        _model_name = model_name
        logger.info("[extractor] memory LLM client initialized model=%s base=%s",
                    model_name, base_url)
        return _client, _model_name


def _strip_fences(text: str) -> str:
    """Strip code fences such as ```json ... ```."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def parse_json(raw: str) -> Optional[dict]:
    """Fault-tolerant parsing of the JSON returned by the LLM."""
    if not raw:
        return None
    s = _strip_fences(raw)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Try to extract the first {...}
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    logger.debug("[extractor] failed to parse JSON: %r", raw[:200])
    return None


async def run_llm_with_prompt(
    prompt: str,
    timeout_s: int,
    *,
    max_tokens: int = 800,
) -> Optional[str]:
    """Call the dedicated memory LLM to run a prompt; returns text or None.

    Wrapping this in another `asyncio.wait_for` externally is fine; the built-in timeout
    here guarantees safety.
    """
    try:
        client, model_name = await _get_client()
    except Exception as exc:
        logger.warning("[extractor] memory client unavailable: %s", exc)
        return None

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=max_tokens,
            ),
            timeout=timeout_s,
        )
        return (resp.choices[0].message.content or "").strip()
    except asyncio.TimeoutError:
        logger.info("[extractor] LLM call timed out after %ds", timeout_s)
        return None
    except Exception as exc:
        logger.warning("[extractor] LLM call failed: %s", exc)
        return None


def fill_prompt(
    template: str,
    user_msg: str,
    assistant_msg: str,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """Fill the prompt template."""
    payload = {
        "user_msg": user_msg,
        "assistant_msg": assistant_msg,
        "curr_date": date.today().isoformat(),
    }
    if extra:
        payload.update(extra)
    try:
        return template.format(**payload)
    except KeyError as exc:
        logger.warning("[extractor] prompt template missing key: %s", exc)
        return template

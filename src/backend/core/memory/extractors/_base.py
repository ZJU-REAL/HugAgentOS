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
            # Deliberately above every caller's own timeout. This client used to
            # cap at 30s while extractors asked for 60, so the HTTP layer gave up
            # first and the caller's budget never applied — a reasoning model
            # simply never finished in time and every extraction returned empty.
            # The real bound belongs to the caller's asyncio timeout, which each
            # extractor sets; this only stops a socket hanging forever.
            timeout=180.0,
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


_THINK_BLOCK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</(think|thinking|reasoning)>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Drop inline chain-of-thought before looking for the answer.

    A reasoning model narrates first and answers last. Its narration quotes the
    prompt's own examples — including their JSON — so anything that reads "the
    first object in the response" reads the model thinking out loud rather than
    its conclusion.

    Handles the **unpaired** closing tag as well: several reasoning endpoints
    start narrating immediately and emit only `</think>` before the answer, with
    no opening tag to pair it with. Matching pairs alone leaves the entire
    narration in place, and its worked examples then win over the real answer.
    """
    cleaned = _THINK_BLOCK_RE.sub("", text)
    matches = list(_THINK_CLOSE_RE.finditer(cleaned))
    if matches:
        cleaned = cleaned[matches[-1].end():]
    return cleaned


def _json_candidates(s: str) -> list:
    """Every balanced ``{...}`` span in the text, in order of appearance.

    Brace-counting rather than a regex: `\\{.*\\}` is greedy and spans from the
    first brace to the last, which on a narrated response is one unparseable
    blob covering the reasoning *and* the answer.
    """
    spans = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(s[start : i + 1])
    return spans


def parse_json(raw: str, *, require_key: Optional[str] = None) -> Optional[dict]:
    """Fault-tolerant parsing of the JSON returned by the LLM.

    Tolerates a reasoning model: the answer is the **last** valid object in the
    response, not the first, because everything before it may be the model
    reciting the prompt's examples while it thinks.

    ``require_key`` is the top-level key the caller expects (``facts`` /
    ``procedures`` / …). Reasoning text is full of half-formed objects — a single
    ``{"field": ..., "value": ...}`` the model was weighing up mid-thought — and
    without this check the last of those parses cleanly and gets returned as the
    result. Requiring the key means an unfinished response yields nothing rather
    than a fragment the writer would happily persist.
    """
    if not raw:
        return None
    s = _strip_fences(_strip_reasoning(raw))

    def _acceptable(value: Any) -> bool:
        return isinstance(value, dict) and (require_key is None or require_key in value)

    try:
        parsed = json.loads(s)
        if _acceptable(parsed):
            return parsed
    except json.JSONDecodeError:
        pass

    for candidate in reversed(_json_candidates(s)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _acceptable(parsed):
            return parsed

    logger.debug("[extractor] failed to parse JSON: %r", raw[:200])
    return None


# A reasoning model spends its budget narrating before it answers. The
# extraction prompts need a few hundred tokens for the answer itself, so a
# budget sized for the answer alone gets consumed by the narration and the reply
# is cut off mid-thought — the JSON never arrives. That failure is invisible:
# every extractor returns None, nothing is written, and the product simply
# reports that no turn ever had anything worth remembering. This floor is what
# makes the memory pipeline work regardless of which model is assigned to the
# memory role.
_REASONING_HEADROOM_TOKENS = 3000


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
                max_tokens=max(max_tokens, _REASONING_HEADROOM_TOKENS),
            ),
            timeout=timeout_s,
        )
        message = resp.choices[0].message
        content = (getattr(message, "content", None) or "").strip()
        if content:
            return content
        # Some OpenAI-compatible reasoning endpoints put everything on
        # `reasoning_content` and leave `content` empty. The answer is in there.
        return (getattr(message, "reasoning_content", None) or "").strip() or None
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

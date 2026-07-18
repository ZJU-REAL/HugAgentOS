"""Shared helpers for distillation LLM callers (skill_distiller / persona_distiller).

Extracted from skill_distiller.py so the per-chat auto distiller and the
persona-level (colleague / personal) distiller share one implementation of:
- model resolution (role 'skill_distiller' → fallback 'main_agent')
- tolerant strict-JSON parsing (<think> blocks, ``` fences, surrounding prose)
- rough cost estimation
- skill dict → SKILL.md rendering
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from core.services.model_config import ModelConfigService, ResolvedModelConfig

logger = logging.getLogger(__name__)


# OpenAI-style rough price table per model. Override via env / admin panel later.
# USD per 1M tokens. Default to a conservative flat rate when model unknown.
PRICE_PER_MTOK: Dict[str, Dict[str, float]] = {
    "default": {"input": 3.0, "output": 15.0},
    "deepseek": {"input": 0.27, "output": 1.1},
    "qwen": {"input": 0.5, "output": 1.5},
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = PRICE_PER_MTOK["default"]
    for key, v in PRICE_PER_MTOK.items():
        if key in (model or "").lower():
            price = v
            break
    return (prompt_tokens * price["input"] + completion_tokens * price["output"]) / 1_000_000.0


def resolve_distiller_model() -> ResolvedModelConfig:
    svc = ModelConfigService.get_instance()
    cfg = svc.resolve("skill_distiller") or svc.resolve("main_agent")
    if cfg is None:
        raise RuntimeError("skill_distiller: no model configured for 'skill_distiller' or 'main_agent'")
    return cfg


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Strip a reasoning model's think blocks and return the body.

    Covers three shapes:
    - closed ``<think>...</think>`` blocks (possibly multiple);
    - only ``</think>`` (some serving stacks swallow the opening tag into the chat template, with the body following the closing tag)
      → take the content after the last closing tag;
    - only ``<think>`` (output was truncated mid-thought by max_tokens, with no body after)
      → discard everything from the opening tag onward.
    """
    t = _THINK_BLOCK_RE.sub("", text)
    m = None
    for m in _THINK_CLOSE_RE.finditer(t):
        pass
    if m is not None:
        t = t[m.end():]
    m = _THINK_OPEN_RE.search(t)
    if m is not None:
        t = t[: m.start()]
    return t.strip()


def extract_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_strict_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON tolerantly: strips <think> blocks, ``` fences, and surrounding prose."""
    if not text:
        return None
    # If the body is already valid JSON, return it directly to avoid corruption from stripping a think tag that happens to appear inside a string value
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    t = strip_think_blocks(text.strip())

    if t.startswith("```"):
        lines = [l for l in t.splitlines() if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()

    try:
        return json.loads(t)
    except Exception:
        pass

    candidate = extract_first_json_object(t)
    if candidate:
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def skill_to_markdown(skill: Dict[str, Any]) -> str:
    """Render a skill dict back to SKILL.md text with YAML frontmatter."""
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None  # type: ignore

    fm = skill.get("frontmatter") or {}
    body = skill.get("instructions_md") or ""

    if yaml is not None:
        fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    else:
        # Fallback: basic key: value formatting (only primitive types)
        parts = []
        for k, v in fm.items():
            if isinstance(v, (list, dict)):
                parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                parts.append(f"{k}: {v}")
        fm_text = "\n".join(parts)

    return f"---\n{fm_text}\n---\n\n{body}\n"

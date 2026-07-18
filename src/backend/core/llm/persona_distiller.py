"""Persona distiller — map/reduce LLM caller for colleague / personal skill jobs.

Two stages (driven by core/services/persona_distillation_service.py):
- map:    summarize_session(trajectory)  → per-session digest JSON (lightweight call)
- reduce: distill_persona(digests, ...)  → final SKILL.md (single synthesis call)

Prompts live as independent parts of the 'distillation' kind:
  session_digest / colleague_distiller / personal_distiller
DB active version (per part) → filesystem fallback.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from core.config.distillation import DistillationConfig, get_config
from core.llm._distill_shared import (
    estimate_cost_usd,
    parse_strict_json,
    resolve_distiller_model,
)
from core.services.distillation_service import Trajectory, trajectory_to_dict

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "prompt_text" / "distillation"

_DIGEST_KEYS = ("topics", "demonstrated_skills", "decision_points", "style_samples", "tool_sequences")


def _load_prompt(part_id: str) -> str:
    try:
        from core.services import prompt_version_service as pvs
        rendered = pvs.render_active_prompt_part("distillation", part_id)
        if rendered:
            return rendered
    except Exception as exc:
        logger.debug("persona_distiller: prompt_version_service miss for %s (%s)", part_id, exc)

    fp = _PROMPT_DIR / f"{part_id}.system.md"
    try:
        return fp.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("persona_distiller: failed to load prompt %s (%s)", part_id, exc)
        raise RuntimeError(f"persona_distiller: prompt '{part_id}' unavailable") from exc


# client is cached by (api_key, base_url, timeout) to reuse the connection pool; auto-swapped on hot model config change
_CLIENT_CACHE: dict[tuple, AsyncOpenAI] = {}


def _get_client(api_key: str, base_url: str, timeout: float) -> AsyncOpenAI:
    key = (api_key, base_url, timeout)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        _CLIENT_CACHE.clear()  # after a config change the old client is no longer needed, avoid accumulation
        _CLIENT_CACHE[key] = client
    return client


async def _call_llm(system_prompt: str, max_tokens: int, cfg: DistillationConfig) -> tuple[str, int, int, str]:
    """One chat call. Returns (content, prompt_tokens, completion_tokens, model_name)."""
    model_cfg = resolve_distiller_model()
    client = _get_client(
        model_cfg.api_key or "DUMMY",
        model_cfg.base_url,
        float(cfg.llm_timeout_s),
    )
    resp = await client.chat.completions.create(
        model=model_cfg.model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请按规则输出严格 JSON。"},
        ],
        temperature=cfg.llm_temperature,
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content or ""
    usage = resp.usage
    ptok = getattr(usage, "prompt_tokens", 0) if usage else 0
    ctok = getattr(usage, "completion_tokens", 0) if usage else 0
    return content, ptok, ctok, model_cfg.model_name


# ─────────────────────── map: per-session digest ────────────────────────


async def summarize_session(
    trajectory: Trajectory,
    cfg: Optional[DistillationConfig] = None,
    hint: str = "",
) -> tuple[Optional[Dict[str, Any]], float]:
    """Summarize one session. Returns (digest_dict | None, cost_usd).

    None means the LLM output couldn't be parsed — caller skips the session
    but still accounts the cost. Retries once on parse failure (reasoning models
    occasionally truncate <think> / drift in format, and a single failure would
    silently drop the entire session out of the distillation scope).
    """
    cfg = cfg or get_config()
    system_prompt = (
        _load_prompt("session_digest")
        .replace(
            "{trajectory_json}",
            json.dumps(trajectory_to_dict(trajectory), ensure_ascii=False, indent=2),
        )
        .replace("{hint}", hint or "(无)")
    )
    cost = 0.0
    content = ""
    parsed: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        content, ptok, ctok, model_name = await _call_llm(
            system_prompt, cfg.persona_digest_max_tokens, cfg
        )
        cost += estimate_cost_usd(model_name, ptok, ctok)
        parsed = parse_strict_json(content)
        if parsed is not None:
            break
        logger.warning(
            "persona_distiller: digest parse failed (attempt %d) chat=%s head=%r",
            attempt + 1, trajectory.chat_id, (content or "")[:120],
        )
    if parsed is None:
        return None, cost

    digest: Dict[str, Any] = {"chat_id": trajectory.chat_id}
    for key in _DIGEST_KEYS:
        val = parsed.get(key)
        digest[key] = [str(x)[:300] for x in val][:5] if isinstance(val, list) else []
    digest["low_value"] = bool(parsed.get("low_value", False))
    return digest, cost


# ─────────────────────── reduce: persona synthesis ────────────────────────


@dataclass
class PersonaDistillOutput:
    skill: Optional[Dict[str, Any]]   # {id, frontmatter, instructions_md}
    digest_text: str                  # persona_digest (colleague) / notes (personal)
    confidence: float
    raw_response: str
    cost_usd: float
    error: Optional[str] = None       # reason for parse failure


async def distill_persona(
    kind: str,
    digests: List[Dict[str, Any]],
    memories_grouped: Optional[Dict[str, Any]],
    assigned_identity: str,
    hint: str,
    sampled_ratio: float,
    cfg: Optional[DistillationConfig] = None,
) -> PersonaDistillOutput:
    """Reduce stage: digests (+ grouped memories) → SKILL.md.

    kind: 'colleague' | 'personal' — picks the prompt template.
    Retries once on JSON-parse failure (costs accumulate).
    """
    cfg = cfg or get_config()
    part_id = "colleague_distiller" if kind == "colleague" else "personal_distiller"
    useful = [d for d in digests if not d.get("low_value")]

    system_prompt = (
        _load_prompt(part_id)
        .replace("{assigned_identity}", assigned_identity or "(由你决定)")
        .replace("{digest_count}", str(len(useful)))
        .replace("{sampled_ratio}", f"{sampled_ratio:.2f}")
        .replace("{digests_json}", json.dumps(useful, ensure_ascii=False, indent=1))
        .replace(
            "{memories_json}",
            json.dumps(memories_grouped or {}, ensure_ascii=False, indent=1)
            if memories_grouped else "(无)",
        )
        .replace("{hint}", hint or "(无)")
    )

    total_cost = 0.0
    content = ""
    parsed: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        content, ptok, ctok, model_name = await _call_llm(
            system_prompt, cfg.persona_reduce_max_tokens, cfg
        )
        total_cost += estimate_cost_usd(model_name, ptok, ctok)
        parsed = parse_strict_json(content)
        if parsed is not None:
            break
        logger.warning(
            "persona_distiller: reduce parse failed (attempt %d) head=%r",
            attempt + 1, (content or "")[:200],
        )

    if parsed is None:
        return PersonaDistillOutput(
            skill=None,
            digest_text="",
            confidence=0.0,
            raw_response=content,
            cost_usd=total_cost,
            error="failed_parse",
        )

    skill = parsed.get("skill") if isinstance(parsed.get("skill"), dict) else None
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    digest_text = str(parsed.get("persona_digest") or parsed.get("notes") or "")[:1000]
    return PersonaDistillOutput(
        skill=skill,
        digest_text=digest_text,
        confidence=confidence,
        raw_response=content,
        cost_usd=total_cost,
        error=None if skill else "missing_skill",
    )

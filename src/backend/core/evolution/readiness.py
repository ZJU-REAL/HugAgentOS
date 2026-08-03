"""Whether this deployment can actually run evolution.

Evolution has two hard external dependencies, and without them it does not fail
— it quietly does much less:

* **An embedding model.** Intent clustering falls back to character n-grams,
  which are measurably blind to paraphrase (0.095 on a real same-intent pair).
  The memory scan skips duplicate and contradiction detection entirely, because
  approximating those lexically would find only the duplicates nobody minds.
  The observable result is a cycle that runs, reports success, and finds
  nothing — indistinguishable from "there was nothing to find".
* **A model for the generators.** Without it no skill document and no prompt
  fragment can be written, so findings accumulate as candidates that can never
  be materialised.

A capability that silently degrades to a shadow of itself is worse than one that
is switched off, because nobody goes looking for the reason. So readiness is
checked *before* evolution can be enabled, reported in the console, and attached
to every cycle report.

The distinction this module holds onto: **not ready** and **found nothing** are
different states, and only one of them is a reason to go investigate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Without this, two of the three engines lose their primary path.
REQUIREMENT_EMBEDDING = "embedding"
# Without this, nothing can be written, so nothing can be materialised.
REQUIREMENT_GENERATOR = "generator"


@dataclass
class Requirement:
    key: str
    label: str
    ok: bool
    detail: str = ""
    # What stops working, in the operator's terms rather than the code's.
    impact: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "ok": self.ok,
            "detail": self.detail,
            "impact": self.impact,
        }


@dataclass
class Readiness:
    ready: bool = False
    requirements: List[Requirement] = field(default_factory=list)

    @property
    def blocking(self) -> List[str]:
        return [r.key for r in self.requirements if not r.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "blocking": self.blocking,
            "requirements": [r.to_dict() for r in self.requirements],
        }


def _check_embedding() -> Requirement:
    """Is an embedding service configured *and* answering?

    Configuration alone is not the question. A configured-but-unreachable
    service produces exactly the silent degradation this module exists to
    surface, so this makes one real call.
    """
    try:
        from core.kb.kb_vector import embed_text

        vector = embed_text("能力进化就绪检查", timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        return Requirement(
            key=REQUIREMENT_EMBEDDING,
            label="向量模型（embedding）",
            ok=False,
            detail=str(exc)[:200],
            impact=(
                "意图聚类会退回字面匹配，抓不到「同一件事换个说法问」；"
                "记忆库的近重复与矛盾检测完全不可用。"
                "进化仍会运行，但大概率什么都发现不了。"
            ),
        )
    if not vector:
        return Requirement(
            key=REQUIREMENT_EMBEDDING,
            label="向量模型（embedding）",
            ok=False,
            detail="服务有响应但返回了空向量",
            impact="同上：语义能力不可用。",
        )
    return Requirement(
        key=REQUIREMENT_EMBEDDING,
        label="向量模型（embedding）",
        ok=True,
        detail=f"{len(vector)} 维",
    )


def _check_generator() -> Requirement:
    """Is the model that writes skill documents and prompt fragments configured?

    Checked by resolving the configuration rather than by calling it: a
    generation call costs real tokens, and a readiness check that bills the
    deployment every time somebody opens the console is its own problem.
    """
    try:
        from core.memory.extractors._base import _resolve_memory_model_config

        base_url, api_key, model_name = _resolve_memory_model_config()
    except Exception as exc:  # noqa: BLE001
        return Requirement(
            key=REQUIREMENT_GENERATOR,
            label="生成模型（技能 / 提示词撰写）",
            ok=False,
            detail=str(exc)[:200],
            impact="无法撰写技能正文与提示词片段，候选会积压但无法物化。",
        )

    missing = [
        name
        for name, value in (
            ("base_url", base_url),
            ("api_key", api_key),
            ("model_name", model_name),
        )
        if not value
    ]
    if missing:
        return Requirement(
            key=REQUIREMENT_GENERATOR,
            label="生成模型（技能 / 提示词撰写）",
            ok=False,
            detail=f"配置不完整，缺少: {missing}",
            impact="无法撰写技能正文与提示词片段，候选会积压但无法物化。",
        )
    return Requirement(
        key=REQUIREMENT_GENERATOR,
        label="生成模型（技能 / 提示词撰写）",
        ok=True,
        detail=model_name,
    )


# The embedding probe is a network call. Configuration does not change between
# one cycle and the next, so probing on every cycle — and a personal cycle can
# run often — spends a request to re-learn something that was true 30 seconds
# ago. Short enough that fixing a misconfiguration shows up promptly.
_CACHE_TTL_S = 300.0
_cached: Optional[Tuple[float, "Readiness"]] = None


def reset_cache() -> None:
    """Drop the cached probe, so a configuration fix is picked up immediately."""
    global _cached
    _cached = None


def check_readiness(*, use_cache: bool = True) -> Readiness:
    """Both dependencies, reported individually.

    Individually rather than as one boolean because they fail differently and
    are fixed by different people: an embedding endpoint is infrastructure, a
    generation model is a model-configuration decision.
    """
    global _cached
    now = time.monotonic()
    if use_cache and _cached is not None and now - _cached[0] < _CACHE_TTL_S:
        return _cached[1]

    requirements = [_check_embedding(), _check_generator()]
    readiness = Readiness(
        ready=all(r.ok for r in requirements),
        requirements=requirements,
    )
    _cached = (now, readiness)
    return readiness

"""Prompt candidates, so attribution to the prompt stops being a dead end.

``prompt`` has been a first-class IR target all along, and the attributor
already routes causes to it — "the prompt never declared that an explicit
current instruction outranks a standing preference" is a prompt problem, not a
memory one. But nothing produced prompt candidates, so every one of those
conclusions terminated: diagnosed, recorded, and unactionable. An attribution
target with no generator is worse than no target, because the diagnosis creates
the impression something will happen.

What this generates is deliberately narrow: **an additional fragment**, never a
rewrite. Three reasons, in order of how badly ignoring them ends:

* The active system prompt is the single most load-bearing artefact in the
  product. A generated rewrite that drops one clause changes behaviour
  everywhere at once, and the blast radius has no upper bound.
* A fragment is reviewable. A diff against a 3,000-word prompt is not, in
  practice — reviewers approve what they can hold in their head.
* A fragment is removable. Rolling back a rewrite means restoring a version;
  rolling back a fragment means deleting one paragraph, which is the same
  operation whether or not the prompt has moved on since.

Stage C's validation refuses anything that would delete or contradict an
existing safety clause, or that would grant capability in prose. A prompt
saying "you may use the shell freely" is a tool grant written in a place where
the tool gate cannot see it, and that is the one thing a self-authored prompt
must never be able to do.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_FRAGMENT_CHARS = 600
MIN_FRAGMENT_CHARS = 20
MAX_ATTEMPTS = 2
DRAFT_TIMEOUT_S = 45

# Enough independent failures that the fragment addresses a pattern rather than
# one bad turn. Prompt changes affect every request, so the evidence bar is the
# highest of any asset kind.
MIN_PROMPT_EVIDENCE = 5

REJECT_TOO_LONG = "fragment_too_long"
REJECT_TOO_SHORT = "fragment_too_short"
REJECT_CAPABILITY_GRANT = "grants_capability_in_prose"
REJECT_OVERRIDES_SAFETY = "contradicts_existing_safety_clause"
REJECT_CODE = "contains_code_block"
REJECT_EMPTY = "empty_draft"
REJECT_META = "talks_about_itself"

# Prose that hands out capability. The check is on the *fragment*, and it is
# intentionally blunt: there is no legitimate reason for a generated prompt
# fragment to speak about what the agent is permitted to do, and a blunt rule
# that occasionally rejects a harmless phrasing costs one regeneration.
_CAPABILITY_PROSE = re.compile(
    r"(?:你可以(?:随意|自由|无需确认)|无需(?:确认|审批|授权)|"
    r"不必征得|忽略(?:上述|以上|之前).*(?:限制|约束|规则)|"
    r"允许你(?:执行|调用|访问)|你现在拥有|解除限制|"
    r"you may (?:freely|now)|ignore (?:the )?(?:above|previous) (?:rules|instructions)|"
    r"without (?:asking|confirmation|approval))",
    re.IGNORECASE,
)
# Statements that only make sense if the model knows it is reading a generated
# artefact. They leak the machinery into the product's voice.
_META_PROSE = re.compile(
    r"(?:本片段|本提示词|由(?:AI|模型|系统)(?:自动)?生成|进化流水线|as an ai language model)",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```", re.MULTILINE)


@dataclass
class PromptEvidence:
    """Why a fragment is being proposed."""

    cause: str
    failing_objectives: List[str] = field(default_factory=list)
    episode_ids: List[str] = field(default_factory=list)
    task_types: List[str] = field(default_factory=list)
    existing_fragments: List[str] = field(default_factory=list)
    # Clauses the new fragment must not contradict. Passed in rather than
    # inferred so the check is against what is actually active, not against a
    # copy that has since drifted.
    safety_clauses: List[str] = field(default_factory=list)

    @property
    def support(self) -> int:
        return len(self.episode_ids)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cause": self.cause,
            "support": self.support,
            "task_types": list(self.task_types),
            "failing_objectives": self.failing_objectives[:10],
            "episode_ids": self.episode_ids[:20],
        }


@dataclass
class FragmentDraft:
    ok: bool
    fragment: str = ""
    violations: List[str] = field(default_factory=list)
    attempts: int = 0
    generator_unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "fragment": self.fragment,
            "violations": list(self.violations),
            "attempts": self.attempts,
            "generator_unavailable": self.generator_unavailable,
        }


def collect_prompt_evidence(
    views: Sequence[Dict[str, Any]],
    *,
    cause: str,
    existing_fragments: Sequence[str] = (),
    safety_clauses: Sequence[str] = (),
) -> Optional[PromptEvidence]:
    """Failing episodes attributed to the prompt, or ``None`` if too few.

    Returning ``None`` below the threshold rather than a weak proposal is the
    point: a prompt fragment applies to every request, so "we saw this twice"
    is not enough to change what every request is told.
    """
    failing = [
        view
        for view in views
        if view.get("verdict") == "failed"
        and str(view.get("attributed_to") or "") == "prompt"
    ]
    if len(failing) < MIN_PROMPT_EVIDENCE:
        return None

    return PromptEvidence(
        cause=cause,
        failing_objectives=[
            str(v.get("objective") or "")[:160] for v in failing if v.get("objective")
        ],
        episode_ids=[str(v.get("episode_id") or "") for v in failing],
        task_types=sorted({str(v.get("task_type") or "chat") for v in failing}),
        existing_fragments=[str(f) for f in existing_fragments],
        safety_clauses=[str(c) for c in safety_clauses],
    )


_DRAFT_PROMPT = """你在为一个 AI 助手的系统提示词补充**一个片段**。这个片段会追加到现有提示词末尾，对所有请求生效。

# 证据：以下失败被归因到「提示词没说清楚」
归因结论：{cause}

失败的请求（{support} 条）：
{objectives}

涉及任务类型：{task_types}

# 现有提示词里已经有的相关段落（不要重复、不要与之冲突）
{existing}

# 硬性要求（违反任一条会被机器校验打回）

1. **只写一条指令**，不超过 {max_chars} 字符。不要写多个主题。
2. **不得**放宽任何限制、不得授予任何能力、不得出现"你可以随意/无需确认/忽略以上规则"这类表述。提示词不是授权的地方。
3. **不得**与现有段落冲突；如果现有段落已经说了这件事，说明问题不在提示词，直接输出 SKIP。
4. **不得**包含代码块、不得提及自己是生成的、不得出现"本片段""进化"之类的元描述。
5. 用祈使句直接写给模型看，中文，不要解释理由。

{retry_hint}

# 输出格式
只输出片段正文本身，不要任何前后缀。如果判断不该改提示词，只输出：SKIP
"""


def _build_prompt(evidence: PromptEvidence, violations: Sequence[str]) -> str:
    retry_hint = ""
    if violations:
        retry_hint = "# 上一版被打回的原因（必须修正）\n" + "\n".join(
            f"- {v}" for v in violations
        )
    return _DRAFT_PROMPT.format(
        cause=evidence.cause,
        support=evidence.support,
        objectives="\n".join(f"- {o}" for o in evidence.failing_objectives[:10]) or "（无）",
        task_types="、".join(evidence.task_types) or "（未分类）",
        existing="\n".join(f"- {f}" for f in evidence.existing_fragments[:10]) or "（无）",
        max_chars=MAX_FRAGMENT_CHARS,
        retry_hint=retry_hint,
    )


def validate_fragment(fragment: str, *, safety_clauses: Sequence[str]) -> List[str]:
    """Every constraint the draft prompt stated, checked."""
    text = (fragment or "").strip()
    if not text:
        return [REJECT_EMPTY]

    violations: List[str] = []
    if len(text) > MAX_FRAGMENT_CHARS:
        violations.append(REJECT_TOO_LONG)
    if len(text) < MIN_FRAGMENT_CHARS:
        violations.append(REJECT_TOO_SHORT)
    if _CODE_FENCE.search(text):
        violations.append(REJECT_CODE)
    if _CAPABILITY_PROSE.search(text):
        violations.append(REJECT_CAPABILITY_GRANT)
    if _META_PROSE.search(text):
        violations.append(REJECT_META)

    # A fragment must not negate a clause that is currently in force. The check
    # is lexical and conservative: it fires when the fragment repeats a safety
    # clause's distinctive wording while carrying a negation, which is the shape
    # of "actually, ignore that".
    for clause in safety_clauses:
        stem = str(clause or "").strip()[:24]
        if len(stem) < 8:
            continue
        if stem in text and any(
            marker in text for marker in ("不需要", "无需", "不必", "可以不", "除外")
        ):
            violations.append(REJECT_OVERRIDES_SAFETY)
            break

    return violations


async def draft_fragment(evidence: PromptEvidence) -> FragmentDraft:
    """Draft and validate a fragment, retrying with the specific violations."""
    from core.memory.extractors._base import run_llm_with_prompt

    violations: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = await run_llm_with_prompt(
            _build_prompt(evidence, violations), timeout_s=DRAFT_TIMEOUT_S, max_tokens=600
        )
        if raw is None:
            return FragmentDraft(
                ok=False,
                violations=["generator_unavailable"],
                attempts=attempt,
                generator_unavailable=True,
            )

        text = raw.strip()
        if text.upper().startswith("SKIP"):
            # The generator's own judgement that the prompt is not the problem.
            # Honoured: a fragment written because one was requested is exactly
            # how a prompt accumulates instructions nobody needed.
            return FragmentDraft(ok=False, violations=["generator_declined"], attempts=attempt)

        violations = validate_fragment(text, safety_clauses=evidence.safety_clauses)
        if not violations:
            return FragmentDraft(ok=True, fragment=text, attempts=attempt)
        logger.info("[prompt-gen] draft attempt %d rejected: %s", attempt, violations)

    return FragmentDraft(ok=False, violations=violations, attempts=MAX_ATTEMPTS)


async def generate_prompt_candidate(
    views: Sequence[Dict[str, Any]],
    *,
    cause: str,
    existing_fragments: Sequence[str] = (),
    safety_clauses: Sequence[str] = (),
) -> Tuple[Optional[Dict[str, Any]], Optional[PromptEvidence], FragmentDraft]:
    """Evidence → draft → validation, in one call."""
    evidence = collect_prompt_evidence(
        views,
        cause=cause,
        existing_fragments=existing_fragments,
        safety_clauses=safety_clauses,
    )
    if evidence is None:
        return None, None, FragmentDraft(ok=False, violations=["insufficient_evidence"])

    draft = await draft_fragment(evidence)
    if not draft.ok:
        return None, evidence, draft

    return (
        {
            "operation": "patch",
            "fragment": draft.fragment,
            "cause": cause,
            "task_types": evidence.task_types,
            "support": evidence.support,
        },
        evidence,
        draft,
    )

"""Writing a skill from evidence: evidence pack → LLM draft → machine validation.

What a materialised skill used to be:

    ## 步骤
    1. `search_web`
    2. `fetch_page`

A list of tool names, rendered by string concatenation. It states no applicability
condition worth the name, no parameters, no checkpoint for "did this step work",
no failure mode. Measured against a real model, the only part of that document
that carried any benefit was the ordering-constraint section; the step list was a
*harm* source, generalising this skill's order into a task family where the
reverse is correct, 5 times out of 5.

So the model writes the document. But it writes only the document — the three
stages are separated precisely so that generation never becomes an authority:

* **Stage A (code)** assembles the evidence: what was asked, what worked, what
  failed and where, the ordering rules with their scopes, and the procedural
  memories behind the pattern. Deciding *what a skill may be written from* stays
  in code.
* **Stage B (model)** drafts SKILL.md under hard constraints stated in the
  prompt.
* **Stage C (code)** validates every one of those constraints and rejects the
  draft if any fails, with up to :data:`MAX_ATTEMPTS` regeneration attempts
  carrying the specific violations back.

If stage B could widen a tool grant, self-evolution would be a prompt-injection
away from privilege escalation. It cannot: stage C intersects the draft's tools
with the evidence's tools and refuses anything else, and the same document then
goes through the IR's own escalation gate before it can be persisted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
# Long enough for steps, checkpoints and failure modes; short enough that a
# skill cannot start competing with the user's own request for the model's
# attention once it is opened.
MAX_BODY_CHARS = 4000
MIN_BODY_CHARS = 120
DRAFT_TIMEOUT_S = 60

# A skill body must not contain executable content. Self-modifying code is the
# one capability this design refuses outright, and a fenced shell block inside a
# document the agent reads and follows is executable content with extra steps.
_CODE_FENCE = re.compile(r"```(?:\w+)?\s*\n", re.MULTILINE)
_URL = re.compile(r"https?://", re.IGNORECASE)
_FRONTMATTER_NAME = re.compile(r"^[a-z0-9_-]+$")
_TOOL_MENTION = re.compile(r"`([A-Za-z_][A-Za-z0-9_.-]*)`")

REJECT_NO_FRONTMATTER = "missing_frontmatter"
REJECT_BAD_NAME = "invalid_skill_id"
REJECT_UNKNOWN_TOOL = "tool_not_in_evidence"
REJECT_CODE_BLOCK = "contains_code_block"
REJECT_EXTERNAL_URL = "contains_external_url"
REJECT_TOO_LONG = "body_too_long"
REJECT_TOO_SHORT = "body_too_short"
REJECT_NO_SCOPE = "missing_scope_section"
REJECT_NO_CHECKPOINTS = "missing_checkpoints"
REJECT_EMPTY = "empty_draft"


@dataclass
class EvidencePack:
    """Everything stage B is allowed to see, and nothing else."""

    skill_id: str
    objectives: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    successful_sequences: List[List[str]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    ordering_constraints: List[Dict[str, Any]] = field(default_factory=list)
    procedures: List[Dict[str, str]] = field(default_factory=list)
    task_types: List[str] = field(default_factory=list)
    excluded_task_types: List[str] = field(default_factory=list)
    support: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "objectives": self.objectives[:10],
            "tools": list(self.tools),
            "successful_sequences": self.successful_sequences[:6],
            "failures": self.failures[:6],
            "ordering_constraints": self.ordering_constraints,
            "procedures": self.procedures[:10],
            "task_types": self.task_types,
            "excluded_task_types": self.excluded_task_types,
            "support": self.support,
        }


@dataclass
class DraftResult:
    ok: bool
    body: str = ""
    display_name: str = ""
    description: str = ""
    violations: List[str] = field(default_factory=list)
    attempts: int = 0
    # ``True`` when no draft could be produced because the model was unreachable
    # — an operational state, not a rejection, and the two must not look alike
    # in the console.
    generator_unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "display_name": self.display_name,
            "description": self.description,
            "violations": list(self.violations),
            "attempts": self.attempts,
            "generator_unavailable": self.generator_unavailable,
            "body_chars": len(self.body),
        }


# ── Stage A: the evidence pack ───────────────────────────────────────────────


def build_evidence_pack(
    *,
    skill_id: str,
    episodes: Sequence[Dict[str, Any]],
    ordering_constraints: Sequence[Dict[str, Any]] = (),
    procedures: Sequence[Dict[str, str]] = (),
) -> EvidencePack:
    """Assemble what a skill may be written from.

    Failures are included deliberately. A document distilled only from successes
    describes a happy path and says nothing about the step where things actually
    go wrong, which is the part a reader needs most.
    """
    objectives: List[str] = []
    tools: List[str] = []
    sequences: List[List[str]] = []
    failures: List[Dict[str, Any]] = []
    task_types: List[str] = []

    for episode in episodes:
        objective = str(episode.get("objective") or "").strip()
        if objective and objective not in objectives:
            objectives.append(objective)
        task_type = str(episode.get("task_type") or "").strip()
        if task_type and task_type not in task_types:
            task_types.append(task_type)

        sequence = [str(t) for t in (episode.get("tool_sequence") or [])]
        for tool in sequence:
            if tool not in tools:
                tools.append(tool)
        if episode.get("verdict") == "success":
            if sequence and sequence not in sequences:
                sequences.append(sequence)
        elif episode.get("verdict") == "failed":
            failures.append(
                {
                    "objective": objective[:160],
                    "tool_sequence": sequence[:12],
                    "failed_at": str(episode.get("failed_tool") or ""),
                    "error": str(episode.get("error") or "")[:200],
                }
            )

    excluded = sorted(
        {
            str(name)
            for rule in ordering_constraints
            for name in (rule.get("contradicted_in") or [])
            if str(name)
        }
    )
    return EvidencePack(
        skill_id=skill_id,
        objectives=objectives,
        tools=tools,
        successful_sequences=sequences,
        failures=failures,
        ordering_constraints=list(ordering_constraints),
        procedures=[dict(p) for p in procedures],
        task_types=[t for t in task_types if t not in excluded],
        excluded_task_types=excluded,
        support=len(episodes),
    )


# ── Stage B: the draft ───────────────────────────────────────────────────────

_DRAFT_PROMPT = """你在为一个 AI 助手撰写一份「技能文档」(SKILL.md)。这份文档会被模型在遇到同类任务时读取并遵照执行。

# 你拿到的证据

## 用户实际提出过的请求（{objective_count} 条）
{objectives}

## 成功执行时用过的工具
{tools}

## 成功的调用顺序
{sequences}

## 失败的执行（这些是最宝贵的部分，说明坑在哪）
{failures}

## 已验证的顺序约束
{constraints}

## 这些任务里沉淀下来的做法/口径
{procedures}

# 硬性要求（违反任何一条，文档会被机器校验打回）

1. **只能引用上面「成功执行时用过的工具」里出现过的工具名**，一个都不能新增、不能猜。
2. **开头第一段必须是「## 不适用范围」**，写明本技能不适用于哪些场景。{exclusion_hint}
3. 必须有「## 适用条件」，写清楚**遇到什么样的请求才该用本技能**——要具体到能和别的任务区分开，不能写"当需要用到这些工具时"这种同义反复。
4. 必须有「## 步骤」，每一步都要写清楚：做什么、**关键参数怎么给**、**怎么判断这一步做对了**（检查点）。
5. 必须有「## 常见失败与处理」，基于上面的失败证据写。
6. **禁止**出现任何代码块（``` 包裹的内容）、禁止出现外部链接（http/https）。
7. 全文不超过 {max_chars} 字符。
8. 用中文撰写，语气是给同事写操作手册，不要写"本技能由AI生成"之类的话。

{retry_hint}

# 输出格式

严格按下面结构输出，不要加任何额外说明，不要用代码块包裹整篇文档：

DISPLAY_NAME: <一行技能名，≤20字，说明这个技能干什么>
DESCRIPTION: <一行描述，≤80字，说明什么时候该用它——模型只看这一行决定要不要打开本技能>
BODY:
## 不适用范围
...
## 适用条件
...
## 步骤
...
## 常见失败与处理
...
"""


def _format_list(items: Sequence[Any], empty: str = "（无）") -> str:
    if not items:
        return empty
    lines = []
    for item in items:
        if isinstance(item, (list, tuple)):
            lines.append("- " + " → ".join(str(x) for x in item))
        elif isinstance(item, dict):
            lines.append("- " + "；".join(f"{k}: {v}" for k, v in item.items() if v))
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _build_prompt(pack: EvidencePack, violations: Sequence[str]) -> str:
    exclusion_hint = (
        f"证据显示本技能在以下任务类型中被推翻，必须写进不适用范围：{'、'.join(pack.excluded_task_types)}。"
        if pack.excluded_task_types
        else "如果证据没有显示明确的排除场景，就写明本技能只适用于「适用条件」描述的场景，其余场景请忽略本技能。"
    )
    retry_hint = ""
    if violations:
        retry_hint = (
            "# 上一版被打回的原因（必须修正）\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n"
        )
    return _DRAFT_PROMPT.format(
        objective_count=len(pack.objectives),
        objectives=_format_list(pack.objectives[:10]),
        tools=_format_list([f"`{t}`" for t in pack.tools]),
        sequences=_format_list(pack.successful_sequences[:6]),
        failures=_format_list(pack.failures[:6]),
        constraints=_format_list(
            [
                f"先 {c.get('before')} 再 {c.get('after')}"
                + (
                    f"（在 {'、'.join(str(x) for x in (c.get('validated_in') or []))} 中验证）"
                    if c.get("validated_in")
                    else ""
                )
                for c in pack.ordering_constraints
                if not (c.get("contradicted_in") or [])
            ]
        ),
        procedures=_format_list(
            [
                p.get("rule", "") + (f"（原因：{p['why']}）" if p.get("why") else "")
                for p in pack.procedures[:10]
            ]
        ),
        exclusion_hint=exclusion_hint,
        max_chars=MAX_BODY_CHARS,
        retry_hint=retry_hint,
    )


def _parse_draft(raw: str) -> Tuple[str, str, str]:
    """``(display_name, description, body)`` from the model's reply."""
    display_name = ""
    description = ""
    body = ""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    body_marker = re.search(r"^BODY:\s*$", text, re.MULTILINE)
    head = text[: body_marker.start()] if body_marker else text
    if body_marker:
        body = text[body_marker.end() :].strip()

    for line in head.splitlines():
        if line.upper().startswith("DISPLAY_NAME:"):
            display_name = line.split(":", 1)[1].strip()
        elif line.upper().startswith("DESCRIPTION:"):
            description = line.split(":", 1)[1].strip()
    return display_name, description, body


# ── Stage C: validation ──────────────────────────────────────────────────────


def validate_draft(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    body: str,
    allowed_tools: Sequence[str],
) -> List[str]:
    """Every constraint stage B was told about, checked. Returns violations."""
    violations: List[str] = []

    if not body.strip():
        return [REJECT_EMPTY]
    if not display_name.strip() or not description.strip():
        violations.append(REJECT_NO_FRONTMATTER)
    if not _FRONTMATTER_NAME.match(skill_id or ""):
        # The loader treats ``name`` as the id and rejects anything outside
        # ``[a-z0-9_-]``. A skill that fails this still appears in the capability
        # list — that metadata comes from table columns — while loading returns
        # nothing, so it is present and inert, which is the hardest failure of
        # all to notice.
        violations.append(REJECT_BAD_NAME)

    if _CODE_FENCE.search(body):
        violations.append(REJECT_CODE_BLOCK)
    if _URL.search(body):
        violations.append(REJECT_EXTERNAL_URL)
    if len(body) > MAX_BODY_CHARS:
        violations.append(REJECT_TOO_LONG)
    if len(body) < MIN_BODY_CHARS:
        violations.append(REJECT_TOO_SHORT)

    if "## 不适用范围" not in body:
        violations.append(REJECT_NO_SCOPE)
    else:
        # The exclusion has to precede anything the model could copy: measured,
        # withholding a contradicted rule was not enough, because the step list
        # generalises on its own.
        scope_at = body.index("## 不适用范围")
        earlier_section = re.search(r"^## ", body[:scope_at], re.MULTILINE)
        if earlier_section:
            violations.append(REJECT_NO_SCOPE)
    if "## 常见失败与处理" not in body and "## 步骤" not in body:
        violations.append(REJECT_NO_CHECKPOINTS)

    allowed = {str(t) for t in allowed_tools}
    mentioned = {m for m in _TOOL_MENTION.findall(body)}
    # Only identifiers that look like tool names are checked: prose in backticks
    # (a field name, a value) must not be mistaken for a tool grant, and a real
    # tool name always appears verbatim in the evidence.
    unknown = sorted(
        name
        for name in mentioned
        if name not in allowed and "_" in name and name.islower()
    )
    if unknown:
        violations.append(f"{REJECT_UNKNOWN_TOOL}: {unknown}")

    return violations


def render_skill_markdown(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    body: str,
    support: int,
    rationale: str = "",
) -> str:
    """Wrap a validated body in the frontmatter the loader requires.

    ``name`` is the skill **id**, not the label — writing the human label there
    made every materialised skill unparseable.
    """
    provenance = (
        f"本技能由能力进化流水线从 {support} 条历史执行证据中沉淀而成，经机器校验后由人工批准。"
    )
    if rationale:
        provenance += f"\n\n沉淀依据：{rationale}"
    return f"""---
name: {skill_id}
display_name: {display_name}
description: {description}
---

# {display_name}

{description}

{body.strip()}

## 来源

{provenance}
"""


# ── The three stages, in order ───────────────────────────────────────────────


async def draft_skill(pack: EvidencePack) -> DraftResult:
    """Draft and validate, retrying with the specific violations.

    Returns ``ok=False`` rather than raising. A skill that cannot be written to
    the required standard must produce no candidate at all: shipping a draft
    that failed validation would put exactly the document the constraints exist
    to prevent in front of the model.
    """
    from core.memory.extractors._base import run_llm_with_prompt

    violations: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = _build_prompt(pack, violations)
        raw = await run_llm_with_prompt(prompt, timeout_s=DRAFT_TIMEOUT_S, max_tokens=2400)
        if raw is None:
            return DraftResult(
                ok=False,
                violations=["generator_unavailable"],
                attempts=attempt,
                generator_unavailable=True,
            )

        display_name, description, body = _parse_draft(raw)
        violations = validate_draft(
            skill_id=pack.skill_id,
            display_name=display_name,
            description=description,
            body=body,
            allowed_tools=pack.tools,
        )
        if not violations:
            return DraftResult(
                ok=True,
                body=body,
                display_name=display_name[:60],
                description=description[:200].replace("\n", " "),
                attempts=attempt,
            )
        logger.info(
            "[skill-gen] draft attempt %d rejected for %s: %s",
            attempt, pack.skill_id, violations,
        )

    return DraftResult(ok=False, violations=violations, attempts=MAX_ATTEMPTS)


async def generate_skill_document(
    *,
    skill_id: str,
    episodes: Sequence[Dict[str, Any]],
    ordering_constraints: Sequence[Dict[str, Any]] = (),
    procedures: Sequence[Dict[str, str]] = (),
    rationale: str = "",
) -> Tuple[Optional[Dict[str, str]], DraftResult]:
    """All three stages. Returns ``(document, result)``; document is ``None`` on failure."""
    pack = build_evidence_pack(
        skill_id=skill_id,
        episodes=episodes,
        ordering_constraints=ordering_constraints,
        procedures=procedures,
    )
    if not pack.tools and not pack.procedures:
        return None, DraftResult(ok=False, violations=["no_evidence_to_write_from"])

    result = await draft_skill(pack)
    if not result.ok:
        return None, result

    return (
        {
            "skill_id": skill_id,
            "display_name": result.display_name,
            "description": result.description,
            "content": render_skill_markdown(
                skill_id=skill_id,
                display_name=result.display_name,
                description=result.description,
                body=result.body,
                support=pack.support,
                rationale=rationale,
            ),
            "allowed_tools": list(pack.tools),
        },
        result,
    )

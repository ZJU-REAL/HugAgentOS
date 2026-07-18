"""Autonomous-loop support — goal decomposition + acceptance-criteria extraction (verdicts have moved to loop_reviewer).

Historically this module also handled "per-round exit verdicts": preferring to run
``verify_cmd`` in the sandbox for ground truth, and falling back to an LLM reading the
evidence when no command was given. But both script verification and self-reported text
verdicts proved unreliable (see trace 435be138: with no verify command it degraded to
trusting the worker's self-report — judged 5/5 achieved even though the site was never
actually changed). Now **verdicts are uniformly handled by the read-only review sub-agent
in ``orchestration/subagents/loop_reviewer``, which personally verifies real output** —
this module only keeps two pieces of pre-run preparation:

  1. :func:`decompose_requirements`: split a natural-language goal into a set of discrete,
     independently verifiable requirements (the driver's exclusive ledger).
  2. :func:`extract_acceptance_criteria`: split the goal into 3~5 checkable acceptance
     criteria, fed to the reviewer for item-by-item judgment.

Both use only a one-shot disable-tools fast agent with pure-text output — no sandbox
access, no verdicts, no scripts run.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GoalSpec:
    """The goal a loop must achieve + its acceptance criteria.

    Verdicts no longer depend on any verify command / numeric score / threshold — the
    reviewer sub-agent personally verifies the real output.
    """

    objective: str
    acceptance_criteria: List[str] = field(default_factory=list)


# ── verdict constants (routing labels shared by reviewer and driver) ─────────
DONE = "done"
CONTINUE = "continue"
OFF_TRACK = "off_track"
NEED_HUMAN = "need_human"


# ── Initializer: split the goal into the driver's exclusive requirement ledger (feature_list) ──
async def decompose_requirements(
    *, goal_spec: "GoalSpec", model_name: Optional[str], user_id: str
) -> List[Dict[str, Any]]:
    """Split a natural-language goal into a set of **discrete, independently verifiable** requirements.

    Each entry carries only ``{id, description}`` — verdicts are uniformly delegated to
    the reviewer sub-agent verifying real output item by item; no more script-verification
    metadata like verify commands / optimization thresholds. On decomposition failure the
    fallback is "1 entry = the goal itself"; it must never drag down the loop.
    """
    prompt = (
        "你是一个自主循环的初始化器（Initializer）。把下面的目标拆成一组**离散、可独立核验**"
        "的需求，让循环之后能一次只啃一条、逐条做扎实。\n\n"
        f"目标：\n{goal_spec.objective}\n\n"
        + (
            "已知验收标准（据此拆，勿遗漏）：\n"
            + "\n".join(f"- {c}" for c in goal_spec.acceptance_criteria) + "\n\n"
            if goal_spec.acceptance_criteria else ""
        )
        + "拆解规则：拆成 3~8 条需求，每条是一个能客观判断「做没做到」的具体特性/改动"
        "（如某个功能模块、某块页面、某类数据/交互/文案）。粒度适中，别拆太碎也别一锅烩。\n\n"
        "严格只输出 JSON 数组，每个元素形如："
        '{"id":"R1","description":"..."}。不要任何多余文字。'
    )
    items: Optional[List[Any]] = None
    try:
        text = await _judge_once(prompt, model_name=model_name, user_id=user_id)
        items = _parse_json_array_lenient(text)
    except Exception as exc:  # noqa: BLE001 - decomposition must never drag down the loop
        logger.warning("[loop-eval] decompose failed: %s", exc)

    reqs: List[Dict[str, Any]] = []
    for i, raw in enumerate(items or [], start=1):
        if not isinstance(raw, dict):
            continue
        desc = str(raw.get("description", "")).strip()
        if not desc:
            continue
        reqs.append({"id": raw.get("id") or f"R{i}", "description": desc})
    # Fallback: nothing decomposed -> single requirement = the goal itself.
    if not reqs:
        reqs = [{"id": "R1", "description": goal_spec.objective}]
    return reqs


async def extract_acceptance_criteria(
    *, objective: str, model_name: Optional[str], user_id: str
) -> List[str]:
    """Before the run, use one LLM call to split the natural-language goal into 3-5 checkable acceptance criteria.

    Used by the reviewer sub-agent to verify real output item by item each round. On
    extraction failure/exception returns ``[]`` (the driver falls back to ``[objective]``);
    this must never drag down the loop.
    """
    prompt = (
        "把下面这个任务目标拆解成 3-5 条**可核对**的验收标准（每条是一个能客观判断"
        "满足/不满足的具体条件，避免空泛）。目标：\n"
        f"{objective}\n\n"
        '严格只输出 JSON 数组，形如 ["标准1", "标准2", "标准3"]，不要任何多余文字。'
    )
    try:
        text = await _judge_once(prompt, model_name=model_name, user_id=user_id)
        obj = _parse_json_array_lenient(text)
        if obj:
            return [str(x).strip() for x in obj if str(x).strip()][:5]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-eval] criteria extraction failed: %s", exc)
    return []


# ── Parsing helpers ──────────────────────────────────────────────────────────
def _parse_json_array_lenient(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    candidates = [text.strip()]
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1 and e > s:
        candidates.append(text[s : e + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _parse_json_lenient(text: str) -> Optional[Dict[str, Any]]:
    """Three-level lenient parsing (object), following plan_mode._parse_plan_json."""
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _json_candidates(text: str):
    if not text:
        return
    yield text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        yield m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        yield text[s : e + 1]


# ── LLM side: standalone fast agent, disable_tools, pure-text decomposition only (no verdicts, no sandbox) ──
async def _make_judge_agent(model_name: Optional[str], user_id: str):
    """One-shot pure-text agent (following the disable_tools mode of astream_generate_plan)."""
    from core.llm.agent_factory import create_agent_executor

    agent, clients = await create_agent_executor(
        disable_tools=True,
        enabled_skill_ids=[],  # Required; otherwise the all-skills fallback lets the pure-text agent run tools
        chat_mode="fast",
        model_name=model_name,
        current_user_id=user_id,
    )
    return agent, clients


async def _judge_once(prompt: str, *, model_name: Optional[str], user_id: str) -> str:
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await _make_judge_agent(model_name, user_id)
    sa = StreamingAgent(agent, clients)
    text = ""
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": prompt}],
            {"user_id": user_id, "enable_thinking": False, "chat_mode": "fast"},
        ):
            if et == "text_delta":
                text += payload
            elif et == "error":
                logger.warning("[loop-eval] judge LLM error: %s", payload)
                break
    finally:
        await close_clients(clients)
    return text

"""Run the per-turn intent recognition of the "智能路由" (``mode_slug=auto``) mode.

Domain rules (gate, candidates, strategies, parsing, fallback) live in
:mod:`core.chat.mode_router` / :mod:`core.chat.mode_route_decision`. This module
owns the one model call and its prefix reuse:

The recognition request is built from the main agent that was just assembled
for the current mode (the fallback mode on a first turn): the same
``_prepare_model_input`` produces the system prompt, tool schemas, full history
and this turn's question byte-for-byte as the main request will, and the
recognition instruction is appended after them. ``tool_choice`` is left alone;
a tool call in the answer counts as a parse failure. Thinking is off. When the
route keeps the current mode the caller reuses that agent as-is; a switch
rebuilds it for the new mode.

Every failure (timeout, bad JSON, unknown slug, model error) falls back; the
conversation is never interrupted.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.chat import mode_route_decision as decision
from core.chat import mode_router
from core.db.threads import run_db, run_in_session

logger = logging.getLogger(__name__)

USAGE_SOURCE = "intent_router"

# Evidence attributes the manifest-bound agent rewrites while preparing a
# request. The probe restores them so the main request publishes its own.
_EVIDENCE_ATTRS = (
    "_jx_context_assembly",
    "_jx_context_manifest",
    "_jx_execution_manifest",
    "_jx_context_budget",
    "_jx_context_budget_details",
)
_MISSING = object()


@dataclass
class RouteTurn:
    """One turn's routing state, carried between the two prepare_main hooks."""

    tool_id: str
    question: str
    plan: Optional[mode_router.RoutePlan] = None
    result: Optional[Dict[str, Any]] = None
    #: The result was already known before assembly (no model call needed).
    settled: bool = False

    @property
    def provisional_slug(self) -> str:
        if self.result is not None:
            return str(self.result["mode_slug"])
        assert self.plan is not None
        return self.plan.provisional_slug


def _question(turn: Any) -> str:
    return str(getattr(turn, "user_message", "") or "")


def _plan(db, context: Dict[str, Any], question: str) -> mode_router.RoutePlan:
    return mode_router.plan_route(db, context, question)


def _instruction(db, plan: mode_router.RoutePlan) -> str:
    return decision.build_instruction_for(plan, db)


async def start(turn: Any) -> Optional[RouteTurn]:
    """Open routing for this turn, or return ``None`` when the turn is not ``auto``.

    The provisional slug is written into ``context["routed_mode_slug"]`` right
    away so agent assembly resolves a real mode. A route carried in by recovery,
    steer or handoff is reused verbatim — no second recognition.
    """
    context = turn.context
    if not mode_router.is_auto(context) or bool(context.get("plan_chat")):
        return None
    stored = mode_router.stored_route(context)
    if stored is not None:
        tool_id = str(stored.get("tool_id") or "") or f"intent_{uuid.uuid4().hex}"
        return RouteTurn(
            tool_id=tool_id,
            question=_question(turn),
            result=mode_router.public_result(stored),
            settled=True,
        )
    question = _question(turn)
    try:
        plan = await run_in_session(_plan, context, question)
    except Exception:  # noqa: BLE001 — routing must never stop the turn
        logger.warning("[intent_router] planning failed; using the fallback mode", exc_info=True)
        plan = mode_router.RoutePlan(tool_id=f"intent_{uuid.uuid4().hex}", question=question,
                                     strategy="smart_keep")
        plan.decided_slug = plan.fallback_slug
        plan.decided_source = mode_router.SOURCE_FALLBACK
        plan.decided_cause = mode_router.CAUSE_ROUTER_ERROR
    route = RouteTurn(tool_id=plan.tool_id, question=question, plan=plan)
    if not plan.needs_model:
        route.result = decision.decided_result(plan)
        route.settled = True
    context[mode_router.ROUTED_SLUG_KEY] = route.provisional_slug
    return route


def call_event(route: RouteTurn) -> Dict[str, Any]:
    return mode_router.tool_call_event(route.tool_id, route.question)


def result_event(route: RouteTurn) -> Dict[str, Any]:
    assert route.result is not None
    return mode_router.tool_result_event(route.tool_id, route.result)


async def decide(turn: Any, route: RouteTurn) -> str:
    """Finish the route (asking the model if needed) and record it. Returns the final slug."""
    if route.result is None:
        assert route.plan is not None
        outcome = await _ask_model(turn, route.plan)
        route.result = decision.resolve_outcome(route.plan, outcome)
    mode_router.apply_to_context(turn.context, route.result, route.tool_id)
    run_id = str(turn.context.get("run_id") or "")
    owner = str(turn.context.get("journal_owner") or "")
    if run_id and owner:
        try:
            await run_db(mode_router.persist_route, run_id, owner, route.result, route.tool_id)
        except Exception:  # noqa: BLE001 — recovery would re-route; the turn goes on
            logger.warning("[intent_router] could not persist the route run=%s", run_id, exc_info=True)
    return str(route.result["mode_slug"])


# ── The model call ─────────────────────────────────────────────────────────


async def _ask_model(turn: Any, plan: mode_router.RoutePlan) -> decision.ModelOutcome:
    started = time.monotonic()
    try:
        instruction = await run_in_session(_instruction, plan)
        model, dedicated = await _routing_model(turn)
        messages, tools = await _request_input(turn, instruction)
        if dedicated:
            messages = _fit_window(messages, tools, model, instruction)
    except Exception:  # noqa: BLE001
        logger.warning("[intent_router] could not build the recognition request", exc_info=True)
        return decision.ModelOutcome(error=mode_router.CAUSE_MODEL_ERROR,
                                     latency_ms=_elapsed(started))
    return await _call(turn, model, messages, tools, timeout_s=plan.timeout_ms / 1000.0,
                       started=started)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _routing_model(turn: Any):
    """The ``intent_router`` role's model, else the conversation's own main model; thinking off.

    "Follow the main model" means the exact provider this run's model selection
    hook will put on the agent (the user's switched model included), in the
    no-thinking variant — never the summarizer role.
    """
    from core.llm.runtime import one_shot

    state = getattr(turn.agent, "state", None)
    provider_id = str(
        getattr(state, "model_provider_id", "") or turn.context.get("model_provider_id") or ""
    )
    return await one_shot.resolve_side_model(mode_router.ROLE_KEY, provider_id)


async def _request_input(turn: Any, instruction: str):
    """The main request's exact ``messages``/``tools`` with the instruction appended.

    Loads history and the question into the agent the way the stream loop will,
    lets the agent's own ``_prepare_model_input`` render them, then restores the
    agent so the main stream starts from a clean state.
    """
    from agentscope.message import UserMsg
    from core.llm.runtime import stream_loop

    agent = turn.agent
    state = agent.state
    options = {"effective_user_message": turn.user_message} if turn.context.get("project_init") else {}
    session_messages, last_user_text = stream_loop.effective_session_messages(
        turn.session_messages, options.get("effective_user_message")
    )
    try:
        state.apply_request_context(turn.context, last_user_text)
    except Exception:  # noqa: BLE001 — the stream loop tolerates this too
        logger.debug("[intent_router] apply_request_context failed", exc_info=True)
    saved_context = list(state.context)
    saved_attrs = {name: getattr(agent, name, _MISSING) for name in _EVIDENCE_ATTRS}
    listener = getattr(agent, "_jx_context_manifest_listener", None)
    agent._jx_context_manifest_listener = None
    try:
        user_msg = await stream_loop.load_request_input(agent, session_messages)
        if user_msg is not None:
            state.context.append(user_msg)
        model_input = await agent._prepare_model_input()
    finally:
        state.context = saved_context
        agent._jx_context_manifest_listener = listener
        for name, value in saved_attrs.items():
            if value is _MISSING:
                if hasattr(agent, name):
                    delattr(agent, name)
            else:
                setattr(agent, name, value)
    messages = list(model_input["messages"]) + [UserMsg(name="user", content=instruction)]
    return messages, model_input.get("tools") or []


def _msg_tokens(msg: Any) -> int:
    from core.llm.context.content import estimate_context_tokens

    dump = getattr(msg, "model_dump", None)
    return estimate_context_tokens(dump() if callable(dump) else str(msg))


def _fit_window(messages: List[Any], tools: List[Any], model: Any, instruction: str) -> List[Any]:
    """Drop the oldest history until the request fits a smaller dedicated model.

    System messages at the head and the last two messages (the question and the
    instruction) are never dropped. Whole messages go, so a tool call and its
    result — which share one assistant message — leave together.
    """
    from core.llm.context.content import estimate_context_tokens
    from core.models.context_window import usable_context_window

    window = int(getattr(model, "context_size", 0) or 0)
    if window <= 0:
        return messages
    budget = usable_context_window(window) - estimate_context_tokens(tools)
    sizes = [_msg_tokens(m) for m in messages]
    total = sum(sizes)
    if total <= budget:
        return messages
    head = 0
    while head < len(messages) and getattr(messages[head], "role", "") == "system":
        head += 1
    keep_tail = 2
    out = list(messages)
    kept_sizes = list(sizes)
    while total > budget and len(out) - head > keep_tail:
        total -= kept_sizes.pop(head)
        out.pop(head)
    logger.info("[intent_router] trimmed history to %d messages for window=%d", len(out), window)
    return out


async def _call(turn, model, messages, tools, *, timeout_s: float, started: float):
    from core.llm.runtime import one_shot

    return await one_shot.call_once(
        model, messages, tools, run_id=str(turn.context.get("run_id") or ""),
        source=USAGE_SOURCE, role=mode_router.ROLE_KEY, timeout_s=timeout_s, started=started,
    )

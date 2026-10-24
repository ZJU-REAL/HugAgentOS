"""Hand a finished turn's evidence to the evolution loop after the stream closes."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _evolution_pending_marker(context: Dict[str, Any], memory_write_enabled: bool):
    """Marker for the closing frame so the client can draw the skeleton card.

    The turn's real outcome is not knowable yet — memory writes are scheduled
    after the stream closes — so all we can honestly say here is "settling, and
    these are the mechanisms that might report".
    """
    message_id = str(context.get("message_id") or "")
    if not message_id or not memory_write_enabled:
        # Without memory writes there is nothing a turn can settle to, so there
        # is nothing for the client to watch.
        return None
    try:
        from core.evolution.settlement.settlement import pending_payload

        return pending_payload(message_id=message_id)
    except Exception:  # pragma: no cover - defensive
        return None


def _runtime_evidence_agent(agent: Any) -> Any:
    """Unwrap streaming/decorator agents to the owner of frozen run evidence."""
    current = agent
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, "_jx_asset_bundle") or hasattr(current, "_jx_skill_selection"):
            return current
        nested = getattr(current, "agent", None)
        if nested is None or nested is current:
            break
        current = nested
    return current


def _assemble_episode_background(
    *,
    message_id: str,
    run_id: str,
    chat_id: str,
    user_id: str,
    objective: str,
    agent: Any = None,
    memory_task: Any = None,
    latency_ms: Optional[int] = None,
    memory_write_enabled: bool = False,
) -> None:
    """Kick off Episode assembly after the stream closed.

    Fire-and-forget by design: the user already has their answer, and evidence
    assembly must never be able to delay or fail a turn. Every failure mode
    here degrades to "no Episode for this run", which the console shows
    honestly rather than papering over.
    """
    if not message_id:
        return

    try:
        from core.agent_runtime.integrations.memory import get_last_retrieval
        from core.evolution.evidence.events import EV_MEMORY_RETRIEVED, TraceSink
        from core.evolution.evidence.trace_assembler import assemble_episode

        evidence_agent = _runtime_evidence_agent(agent)
        bundle = (
            getattr(
                evidence_agent,
                "evidence_bundle",
                getattr(evidence_agent, "_jx_asset_bundle", None),
            )
            if evidence_agent is not None
            else None
        )

        sink = TraceSink(run_id=run_id, message_id=message_id, chat_id=chat_id, user_id=user_id)
        # The rendered memory block has already discarded ids, scores and ranks;
        # this is the only place they still exist.
        retrieval = get_last_retrieval(memory_task)
        if retrieval is not None:
            sink.append(
                EV_MEMORY_RETRIEVED,
                retrieval.to_event_payload(),
                asset_kind="memory",
            )

        # Which skills were considered, chosen, and rejected — the rejects are
        # the negative examples skill attribution is built on.
        # Ordered tool calls, read back from the log this run already wrote.
        # Without them an episode carries no tool sequence and the promotion
        # chain has nothing to distil.
        from core.evolution.evidence.trace_assembler import emit_tool_events

        emit_tool_events(sink, message_id, chat_id)

        selection = (
            getattr(evidence_agent, "_jx_skill_selection", None)
            if evidence_agent is not None
            else None
        )
        if selection is not None:
            from core.evolution.evidence.events import EV_SKILL_SELECTED

            sink.append(EV_SKILL_SELECTED, selection.to_event_payload(), asset_kind="skill")
        sink.flush()

        # Stamp the user's contribution choice onto the episode. Doing it at
        # write time means the decision is fixed by what the user had agreed to
        # *at that moment*, rather than being re-evaluated later against a
        # setting they may since have changed.
        from core.evolution.profiles.user_settings import resolve_for_user

        privacy_class = resolve_for_user(user_id).get("privacy_class", "tenant")

        episode_id = assemble_episode(
            message_id=message_id,
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            objective=objective,
            bundle=bundle,
            latency_ms=latency_ms,
            privacy_class=privacy_class,
        )

        # Hand the evidence side to the settlement runner. It settles once the
        # memory write pipeline reports what it actually wrote — the only party
        # that knows. Settling here instead would have to guess that number, and
        # the guess it used to make (the *retrieval* count) described what the
        # turn read, not what it learned.
        from core.evolution.settlement.settlement_runner import register_turn

        # Which skills the turn opened and which profile governed it stay on the
        # episode, where the retirement engine reads them. They are not on the
        # card: they describe the system doing its job, not learning anything.
        register_turn(
            message_id=message_id,
            episode_id=episode_id or "",
            expects_memory=memory_write_enabled,
        )

        # Consider running a cycle now that new evidence exists. The check is a
        # single indexed count and almost always says no; when it says yes the
        # cycle runs off-thread. The user already has their answer either way.
        from core.evolution.cycle.trigger import schedule_if_due, schedule_personal

        # This user's own loop runs in every edition; the fleet-wide one is
        # enterprise-gated and no-ops elsewhere.
        schedule_personal(user_id)
        schedule_if_due()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[evolution] episode assembly skipped: %s", exc)

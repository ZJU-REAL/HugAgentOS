"""Run the per-answer evaluation gate: one lightweight call deciding whether to ask for a rating.

Domain rules (parsing, fallback causes, storage shape, read-side lists) live in
:mod:`core.chat.evaluation_gate`. This module owns the background task and the model call.

Input path: the transcript is rebuilt from the chat's stored history, not from the main
request. By the time the turn completes, the chat worker holds no agent handle (``TurnState``
carries none), the runtime has already shut the streaming agent down before yielding the
``meta`` chunk, and the stored answer can differ from the model's last message (ontology
output review may replace it). So the main request's prefix cannot be reproduced reliably
here; the gate pays a short, capped transcript instead of a prefix-cache hit.

The model follows the intent router's choice: the ``evaluation_gate`` role when assigned,
otherwise this chat's actual main model, always with thinking off (``fast``). Every failure —
timeout, model error, unparseable output, a tool call — stores ``need_evaluation: true`` with
``source: "fallback"``; nothing here ever raises into the turn.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from core.chat import evaluation_gate as gate
from core.config.store import evaluation_gate_options as gate_options
from core.db.threads import run_in_session
from core.llm.runtime import one_shot

logger = logging.getLogger(__name__)


@dataclass
class GateRequest:
    run_id: str
    chat_id: str
    message_id: str
    timeout_ms: int
    instruction: str
    blocks: List[str]


def spawn(*, run_id: str, chat_id: str, message_id: str, provider_id: str = "") -> None:
    """Decide the gate for a finished answer in the background (never blocks the stream)."""
    if not (run_id and chat_id and message_id):
        return
    from core.infra.background import spawn as spawn_task

    spawn_task(
        run_gate(run_id=run_id, chat_id=chat_id, message_id=message_id, provider_id=provider_id),
        name=f"evaluation_gate:{run_id}",
    )


def _principles(db) -> str:
    """The prompt center's principles (the shipped default when nothing is stored).

    ``render_kind_segment`` already falls back to the on-disk default when the stored version is
    missing or fails to render; an empty result means even that is gone, which is worth a warning:
    the model would judge from the output format alone.
    """
    from core.llm import prompt_rendering

    try:
        text = prompt_rendering.render_kind_segment(gate.PROMPT_KIND, db=db).strip()
    except Exception:  # noqa: BLE001
        logger.warning("[evaluation_gate] prompt render failed", exc_info=True)
        return ""
    if not text:
        logger.warning("[evaluation_gate] prompt rendered empty; judging without principles")
    return text


def _prepare(db, run_id: str, chat_id: str, message_id: str) -> Optional[GateRequest]:
    """The request for this answer, or ``None`` when it is not gated (switch off, not evaluable).

    Re-checks :func:`gate.gate_expected` — the predicate completion used to decide the spawn
    (one indexed row lookup) — because the switch may have been turned off in between.
    """
    if not gate.gate_expected(db, run_id):
        return None
    options = gate_options.evaluation_gate_options()
    entries = gate.load_transcript(db, chat_id, message_id)
    if not any(e.is_target for e in entries):
        return None
    return GateRequest(
        run_id=run_id, chat_id=chat_id, message_id=message_id, timeout_ms=options.timeout_ms,
        instruction=gate.build_instruction(_principles(db)),
        blocks=gate.render_transcript(entries),
    )


def _store(db, run_id: str, message_id: str, decided: dict) -> bool:
    from core.chat.service import ChatService

    # A rerun reuses the message id; a decision about the previous answer must not land on it.
    if not gate.latest_run_is(db, message_id, run_id):
        return False
    return ChatService(db).update_message_extra_data(message_id, {gate.STORAGE_KEY: decided})


async def run_gate(*, run_id: str, chat_id: str, message_id: str,
                   provider_id: str = "") -> Optional[dict]:
    """Decide and store the gate for one answer. Returns the stored result (``None`` if skipped)."""
    try:
        request = await run_in_session(_prepare, run_id, chat_id, message_id)
    except Exception:  # noqa: BLE001 — without a request there is nothing to store
        logger.warning("[evaluation_gate] could not prepare run=%s", run_id, exc_info=True)
        return None
    if request is None:
        return None
    outcome = await _ask_model(request, provider_id)
    decided = gate.resolve_outcome(outcome)
    try:
        stored = await run_in_session(_store, run_id, message_id, decided)
    except Exception:  # noqa: BLE001 — unstored reads as "needs evaluation" later
        logger.warning("[evaluation_gate] could not store run=%s", run_id, exc_info=True)
        return None
    logger.info("[evaluation_gate] run=%s need=%s source=%s cause=%s latency=%sms stored=%s",
                run_id, decided["need_evaluation"], decided["source"], decided["cause"],
                decided["latency_ms"], stored)
    return decided if stored else None


# ── The model call ─────────────────────────────────────────────────────────


async def _ask_model(request: GateRequest, provider_id: str) -> one_shot.ModelOutcome:
    started = time.monotonic()
    try:
        model, _dedicated = await one_shot.resolve_side_model(gate.ROLE_KEY, provider_id)
        messages = _messages(request, model)
    except Exception:  # noqa: BLE001
        logger.warning("[evaluation_gate] could not build the request", exc_info=True)
        return one_shot.ModelOutcome(error=one_shot.CAUSE_MODEL_ERROR,
                                     latency_ms=one_shot.elapsed_ms(started))
    return await one_shot.call_once(
        model, messages, [], run_id=request.run_id, source=gate.USAGE_SOURCE, role=gate.ROLE_KEY,
        timeout_s=request.timeout_ms / 1000.0, started=started,
    )


def _messages(request: GateRequest, model: Any) -> List[Any]:
    from agentscope.message import SystemMsg, UserMsg

    blocks = _fit_window(request.instruction, request.blocks, model)
    return [SystemMsg(name="system", content=request.instruction),
            UserMsg(name="user", content=gate.build_prompt(blocks))]


def _fit_window(instruction: str, blocks: List[str], model: Any) -> List[str]:
    """Drop the oldest transcript blocks until the request fits the model's window.

    The last two blocks (this turn's question and the answer being judged) always stay.
    """
    from core.llm.context.content import estimate_context_tokens
    from core.models.context_window import usable_context_window

    window = int(getattr(model, "context_size", 0) or 0)
    if window <= 0:
        return blocks
    budget = usable_context_window(window) - estimate_context_tokens(instruction) - 200
    sizes = [estimate_context_tokens(b) for b in blocks]
    total = sum(sizes)
    out = list(blocks)
    while total > budget and len(out) > 2:
        total -= sizes.pop(0)
        out.pop(0)
    return out

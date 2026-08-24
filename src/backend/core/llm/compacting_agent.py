# -*- coding: utf-8 -*-
"""AgentScope step-boundary compaction backed by the shared checkpoint pipeline.

Only agents with ``state.chat_id`` publish checkpoints. Agents without a chat
ID still compact their live context but do not modify a parent conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agentscope.agent import Agent, ContextConfig

from core.llm.manifest_agent import ManifestBoundAgent

logger = logging.getLogger(__name__)

# Appended when dropped context is available from the sandbox offloader.
_OFFLOAD_REMINDER = (
    "\n<system-reminder>The compressed context is offloaded to '{path}', "
    "you can refer to it when needed.</system-reminder>"
)


@dataclass
class _ContextObservation:
    """Server-reported token usage and the context length it measured."""

    tokens: int
    context_len: int


class CompactingAgent(ManifestBoundAgent):
    """Agent whose step-boundary compaction uses the shared persisted engine.

    Framework fallbacks call ``Agent.compress_context`` directly because
    ``ManifestBoundAgent`` intentionally implements that method as a no-op.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._jx_observation: Optional[_ContextObservation] = None
        # Resolved once by agent_factory to avoid a config DB read per ReAct step.
        self._jx_trigger_ratio: Optional[float] = None
        # Prevent repeated compaction when an oversized context cannot shrink.
        self._jx_compacted_at_len: Optional[int] = None

    # ── Token metering ───────────────────────────────────────────────────────

    def observe_context_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Record provider-reported context occupancy for the last model call."""
        total = int(prompt_tokens or 0) + int(completion_tokens or 0)
        if total <= 0:
            return
        self._jx_observation = _ContextObservation(
            tokens=total, context_len=len(self.state.context)
        )

    async def _measure_context_tokens(self) -> int:
        """Current context occupancy: real usage + estimate of what came after it.

        Falls back to AgentScope's byte estimate over the whole prompt when the
        provider has not reported usage yet (the first reasoning step of a turn,
        or an endpoint that omits usage).
        """
        obs = self._jx_observation
        if obs is not None and 0 <= obs.context_len <= len(self.state.context):
            trailing = self.state.context[obs.context_len :]
            return obs.tokens + self._estimate_msgs_tokens(trailing)
        kwargs = await self._prepare_model_input()
        return int(await self.model.count_tokens(**kwargs))

    @staticmethod
    def _estimate_msgs_tokens(msgs: List[Any]) -> int:
        from core.services.compaction_service import estimate_history_tokens

        if not msgs:
            return 0
        return estimate_history_tokens([msg_to_history_dict(m) for m in msgs])

    # ── MidTurn compaction ───────────────────────────────────────────────────

    async def compress_context(self, context_config: ContextConfig | None = None) -> None:
        """Compact at a ReAct step boundary, falling back to AgentScope on failure."""
        cfg: ContextConfig = context_config or self.context_config

        from core.config.settings import settings

        if not settings.compaction.enabled:
            # Disabling checkpoints must not disable live overflow protection.
            try:
                await Agent.compress_context(self, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[compaction] framework compression failed: %r", exc)
            return

        try:
            limit, tokens = await self._should_compact()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[compaction] mid-turn measurement failed: %r", exc)
            return
        if limit is None or tokens < limit:
            return

        if not self.state.context:
            # Only the system prompt remains, so there is nothing to remove.
            logger.warning(
                "[compaction] mid-turn skipped: context empty, %d tokens over limit %d",
                tokens,
                limit,
            )
            return

        if (
            self._jx_compacted_at_len is not None
            and len(self.state.context) <= self._jx_compacted_at_len
        ):
            logger.warning(
                "[compaction] mid-turn skipped: context did not grow since the last "
                "compaction (len=%d), a further summary cannot shrink it",
                len(self.state.context),
            )
            return

        chat_id = str(getattr(self.state, "chat_id", "") or "")
        logger.info(
            "[compaction] mid-turn triggered chat=%s tokens=%d limit=%d msgs=%d",
            chat_id or "(none)",
            tokens,
            limit,
            len(self.state.context),
        )

        history = self._history_for_summary()
        try:
            from core.services.compaction_service import run_mid_turn_compaction

            replacement = await run_mid_turn_compaction(chat_id, history)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[compaction] mid-turn engine failed: %r", exc)
            replacement = None

        if not replacement:
            logger.warning("[compaction] mid-turn falling back to framework compression")
            try:
                await Agent.compress_context(self, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[compaction] framework compression failed too: %r", exc)
                return
            self._jx_observation = None
            self._jx_compacted_at_len = len(self.state.context)
            return

        await self._apply_replacement(replacement)

    async def _should_compact(self) -> tuple[Optional[int], int]:
        """Return ``(limit, measured_tokens)``; ``limit`` is None when undeterminable."""
        from core.services.compaction_service import resolve_token_limit

        window = int(getattr(self.model, "context_size", 0) or 0)
        limit = resolve_token_limit(window, ratio=self._jx_trigger_ratio)
        if limit is None:
            return None, 0
        return limit, await self._measure_context_tokens()

    def _history_for_summary(self) -> List[Dict[str, Any]]:
        """Return live context as summary input, including any fallback summary."""
        history: List[Dict[str, Any]] = []
        if self.state.summary:
            history.append({"role": "user", "content": self.state.summary})
        history.extend(msg_to_history_dict(m) for m in self.state.context)
        return history

    async def _apply_replacement(self, replacement: List[Dict[str, Any]]) -> None:
        """Install the compacted history, spilling what was dropped to the sandbox.

        The offload pointer is added to the in-memory copy only — the checkpoint
        was persisted before the spill, and deliberately stays clean: the sandbox
        session that owns that path need not outlive this turn, and a checkpoint
        that points at a vanished file is worse than one that never mentions it.
        """
        from core.llm.message_compat import session_to_msgs

        dropped = list(self.state.context)
        replacement = [dict(m) for m in replacement]

        if self.offloader and dropped:
            try:
                path = await self.offloader.offload_context(self.state.session_id, msgs=dropped)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[compaction] offload failed: %r", exc)
            else:
                if path and replacement:
                    tail = replacement[-1]
                    tail["content"] = f"{tail.get('content') or ''}" + _OFFLOAD_REMINDER.format(
                        path=path
                    )

        # Keep the summary trailing, matching checkpoint replay order.
        self.state.summary = ""
        self.state.context = session_to_msgs(replacement)
        self._jx_observation = None
        self._jx_compacted_at_len = len(self.state.context)
        logger.info(
            "[compaction] mid-turn applied msgs=%d→%d", len(dropped), len(self.state.context)
        )


def msg_to_history_dict(msg: Any) -> Dict[str, Any]:
    """Convert a ``Msg`` while preserving tool-call and tool-result blocks."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        blocks: List[Any] = []
        for block in content:
            if hasattr(block, "model_dump"):
                blocks.append(block.model_dump())
            else:
                blocks.append(block)
        return {"role": getattr(msg, "role", "user"), "content": blocks}
    return {"role": getattr(msg, "role", "user"), "content": content}

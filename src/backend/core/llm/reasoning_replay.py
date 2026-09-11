"""Provider-aware reasoning replay without mutating stored history."""

from __future__ import annotations

from typing import Any

from agentscope.formatter import FormatterBase
from agentscope.message import ContentBlock, Msg, TextBlock, ThinkingBlock


class ReasoningReplayMixin(FormatterBase):
    replay_provider: str = ""
    replay_model: str = ""
    replay_protocol: str = ""

    def prepare_replay(self, msgs: list[Msg]) -> list[Msg]:
        # Unbound formatters are retained for standalone SDK-style use. Model
        # constructors bind all three fields before sending real requests.
        if not self.replay_provider:
            return msgs
        out = []
        for msg in msgs:
            blocks: list[ContentBlock] = []
            for block in msg.content:
                if not isinstance(block, ThinkingBlock):
                    blocks.append(block)
                    continue
                provider = str(getattr(block, "provider", "") or "")
                model = str(getattr(block, "model", "") or "")
                protocol = str(getattr(block, "protocol", "") or "")
                compatible = (
                    provider == self.replay_provider
                    and model == self.replay_model
                    and (not protocol or protocol == self.replay_protocol)
                )
                if compatible:
                    if self.replay_protocol == "anthropic_messages" and not getattr(
                        block, "signature", None
                    ):
                        raise ValueError(
                            "Anthropic reasoning from the current model is missing its signature"
                        )
                    blocks.append(block)
                elif block.thinking:
                    # Explicit historical reference, never a native reasoning
                    # block or a signature fabricated for the target provider.
                    blocks.append(
                        TextBlock(
                            text="[Historical reasoning from another or unknown model]\n"
                            + block.thinking
                        )
                    )
            if blocks:
                out.append(msg.model_copy(update={"content": blocks}))
        return out

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        # Concrete SDK formatter follows this mixin in each bound class MRO.
        return await super().format(self.prepare_replay(msgs))  # type: ignore[safe-super]


def stamp_reasoning_origin(content: Any, model: Any) -> None:
    """Stamp newly completed output before the SDK adds it to live context."""
    for block in content or []:
        if isinstance(block, ThinkingBlock):
            setattr(block, "provider", str(getattr(model, "provider_id", "") or ""))
            setattr(block, "model", str(getattr(model, "model", "") or ""))
            setattr(block, "protocol", str(getattr(model, "wire_protocol", "") or ""))

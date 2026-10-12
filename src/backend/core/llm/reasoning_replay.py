"""Provider-aware reasoning replay without mutating stored history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agentscope.message import ContentBlock, Msg, ThinkingBlock

if TYPE_CHECKING:  # 类型上它确实是个 formatter；运行时刻意不继承（见类文档）
    from agentscope.formatter import FormatterBase as _MixinBase
else:
    from pydantic import BaseModel as _MixinBase


class ReasoningReplayMixin(_MixinBase):
    """只贡献「思考回放」这一件行为，不带任何 formatter 配置字段。

    继承 ``FormatterBase`` 会连它的字段默认值一起背过来。本 mixin 在各绑定类的
    MRO 里排在具体厂商 formatter **前面**，pydantic 于是用它这份继承来的默认值
    覆盖厂商自己的——其中 ``input_types`` 的基类默认是最保守的 ``["text/plain"]``，
    把 OpenAI / Gemini / DashScope / Ollama 各自声明的 ``image/*`` 全盖成了纯文本，
    工具返回的图片因此在发出前被静默丢弃。继承 ``BaseModel`` 后本类不再声明
    ``input_types``，该字段回落到厂商 formatter 自己的声明。
    """

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
                # Foreign or unknown reasoning stays in stored/display history.
                # Promoting it to assistant text makes private reasoning appear
                # to be a previous answer and invites the next model to repeat it.
            if blocks:
                out.append(msg.model_copy(update={"content": blocks}))
        return out

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        # Concrete SDK formatter follows this mixin in each bound class MRO.
        return cast(
            list[dict[str, Any]],
            await super().format(self.prepare_replay(msgs)),  # type: ignore[safe-super]
        )


def stamp_reasoning_origin(content: Any, model: Any) -> None:
    """Stamp newly completed output before the SDK adds it to live context."""
    for block in content or []:
        if isinstance(block, ThinkingBlock):
            setattr(block, "provider", str(getattr(model, "provider_id", "") or ""))
            setattr(block, "model", str(getattr(model, "model", "") or ""))
            setattr(block, "protocol", str(getattr(model, "wire_protocol", "") or ""))

"""Shared: L3 placeholder-summary fallback when the compression call (generate_structured_output) fails.

AgentScope 2.0's context compression goes through ``self.model.generate_structured_output()``;
there is no separate compression_model slot and no on_compress_context middleware. If that call
raises — e.g. because upstream returns malformed JSON — it crashes ``agent.reply()`` entirely.
This mixin wraps it in try/except: on failure it returns a placeholder summary with fixed fields,
letting compress_context write the summary normally and reply continue.

All custom ChatModel subclasses (OpenAI-compatible / native vendor / litellm) inherit this mixin,
avoiding per-class duplication.
"""

from __future__ import annotations

import logging
from typing import Any

from agentscope.message import Msg
from agentscope.model import StructuredResponse

logger = logging.getLogger(__name__)

# The content fields correspond to summary_schema (task_overview / current_state / ...).
L3_SYNTHETIC_METADATA: dict[str, str] = {
    "task_overview": "用户的原始任务（详细历史因摘要服务暂不可用已舍弃）",
    "current_state": "已完成若干工具调用，工具返回结果保留在最近上下文中",
    "important_discoveries": "早期对话与工具结果因压缩调用失败已无法访问",
    "next_steps": "请基于当前可见的最近上下文直接给出用户最终答复，不要再调用工具",
    "context_to_preserve": "保持中文回复；遵循用户最初提出的输出要求",
}


class StructuredFallbackMixin:
    """Provides an L3 fallback for generate_structured_output to ChatModel subclasses.

    Must be placed before the actual model class in the MRO
    (``class Foo(StructuredFallbackMixin, SomeChatModel)``), so that this class's
    ``super()`` points to the real model implementation.
    """

    async def generate_structured_output(  # type: ignore[override]
        self,
        messages: "list[Msg]",
        structured_model: Any,
        **kwargs: Any,
    ) -> StructuredResponse:
        try:
            return await super().generate_structured_output(  # type: ignore[misc]
                messages, structured_model, **kwargs
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[safe-compress] generate_structured_output 失败 (%s) — 返回 L3 占位摘要。",
                e,
            )
            return StructuredResponse(content=dict(L3_SYNTHETIC_METADATA))

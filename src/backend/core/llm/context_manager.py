"""Context window budget manager.

Trims history messages against a budget before they are loaded into Agent Memory,
preventing overruns of the model's context limit.
Complementary to AgentScope's CompressionConfig:
- ContextWindowManager: trims before loading (coarse-grained, overflow prevention)
- CompressionConfig: automatic compression during inference (fine-grained, preserves key information)

Layered defenses provided by this module:

    Layer C  compress_oversized_user_message
             Detects whether the last user message alone exceeds the model's safe budget;
             if so, applies head+tail truncation + a placeholder notice so the downstream
             API doesn't reject the request outright.

    base     trim_history
             Keeps messages from newest to oldest within the token budget; the current-turn
             user message is force-kept (the force_keep_from anchor) and never dropped.
             Multimodal content goes through estimate_message_tokens for per-block
             estimation, so base64 is never counted as text.

The external entry point ``ContextWindowManager.manage_context`` runs both layers in one pass.

Note: cross-turn session_messages **do not include tool-call history** (see
``_load_session_messages``: only the content column is read; the tool_calls column is dropped),
so the "old tool_results eating the whole budget" problem **cannot occur** within this
module's scope. Within-turn tool result bloat is handled by the 2.0 built-in ``ContextConfig``
(trigger_ratio / tool_result_limit) + the L3 fallback of
``StructuredFallbackMixin.generate_structured_output`` (see core/llm/providers/_fallback.py),
and is out of scope here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 2.5  # average estimate for Chinese text

# Token estimation constant for vision blocks. Vision models are actually billed per patch,
# independent of base64 length; a conservative upper bound is enough, avoiding counting
# base64 data as text.
VISION_BLOCK_TOKENS = 800

# Layer C default parameter: max token ratio the current user message may occupy (relative to the model context)
USER_MESSAGE_MAX_RATIO = 0.6


def resolve_model_context_window(model_name: str) -> int:
    """Read the context window size from the Config admin platform's model configuration.

    Data source: ModelProvider.extra_config.context_length (DB-driven, single source of truth).
    **No default fallback**: unconfigured / empty model name / query failure all raise
    ``ValueError``, forcing every model's real context_length to be filled in under
    Config admin → model configuration — the silent 128k fallback once caused a real
    256k model to repeatedly trigger compression at half its window.
    Callers that can tolerate "no window, just skip" (e.g. the compression-trigger check)
    should try/except themselves.
    """
    if not model_name:
        raise ValueError(
            "无法解析模型上下文窗口：模型名为空。请检查模型配置（Config 后管 → 模型配置）。"
        )

    try:
        from core.services.model_config import ModelConfigService
        ctx_len = ModelConfigService.get_instance().get_context_length_by_model_name(model_name)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"查询模型 '{model_name}' 的上下文窗口配置失败：{exc}"
        ) from exc
    if ctx_len and ctx_len > 0:
        return ctx_len

    raise ValueError(
        f"模型 '{model_name}' 未配置 context_length（上下文窗口）。"
        "请到 Config 后管平台 → 模型配置中为该模型补齐真实上下文长度。"
    )


def estimate_tokens(text: str) -> int:
    """Estimate token count of text. Chinese is roughly 2.5 characters/token."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_message_tokens(content: Any) -> int:
    """Estimate the token count of a single message's content, handling multimodal block lists correctly.

    OpenAI / Anthropic message content may be:
    - str: plain text, estimated by character count
    - list[dict]: a multimodal block list (text / image / tool_use / tool_result ...)

    A naive ``estimate_tokens(str(content))`` would count the base64 data inside image
    blocks too — a 100KB image would be estimated at ~40K tokens, causing messages with
    images to be wrongly dropped during trimming. Here we accumulate per block type.
    """
    if not content:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                # 2.0: content blocks are pydantic objects → convert to dict to reuse the logic below
                if hasattr(block, "model_dump"):
                    block = block.model_dump()
                else:
                    total += estimate_tokens(str(block))
                    continue
            btype = str(block.get("type", "")).lower()
            if btype in ("text", "output_text"):
                total += estimate_tokens(str(block.get("text", "")))
            elif btype in ("image", "image_url", "data"):
                total += VISION_BLOCK_TOKENS
            elif btype in ("tool_use", "tool_call"):
                args = block.get("input") or block.get("arguments") or {}
                total += estimate_tokens(str(block.get("name", "")))
                total += estimate_tokens(str(args))
            elif btype == "tool_result":
                # AgentScope's ToolResultBlock carries its payload under
                # ``output`` (not ``content``); accept either for safety.
                payload = block.get("output")
                if payload is None:
                    payload = block.get("content", "")
                total += estimate_message_tokens(payload)
            else:
                # Unknown block: only take the obvious text fields; fall back to the whole block as a string
                text_field = block.get("text") or block.get("content")
                if isinstance(text_field, (str, list)):
                    total += estimate_message_tokens(text_field)
                else:
                    total += estimate_tokens(str(block))
        return total
    return estimate_tokens(str(content))


def _truncate_head_tail(text: str, max_chars: int) -> str:
    """Lossy "head + tail + omission notice" truncation of long text, preserving the semantic information at both ends."""
    if len(text) <= max_chars:
        return text
    if max_chars < 200:
        # Too small — just truncate from the head
        return text[:max_chars] + "…"
    head = int(max_chars * 0.6)
    tail = int(max_chars * 0.3)
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}"
        f"\n\n[…省略 {omitted:,} 字符…]\n\n"
        f"{text[-tail:]}"
    )


# ---------------------------------------------------------------------------
# Layer C: compress the current user message when it is too large
# ---------------------------------------------------------------------------

def compress_oversized_user_message(
    message: Dict[str, Any],
    max_tokens: int,
) -> Tuple[Dict[str, Any], bool]:
    """Apply head+tail truncation if a single user message exceeds ``max_tokens``.

    Implementation details:
    - content is str: apply head/tail truncation to the whole string directly
    - content is list[block]: find the text/output_text block with the largest cumulative
      character count and truncate its text; other blocks (image, tool_use, etc.) are kept as-is
    - Always returns a new object; the original message is never mutated

    Returns:
        (new_message, was_compressed)
    """
    if not message:
        return message, False

    tokens = estimate_message_tokens(message.get("content"))
    if tokens <= max_tokens:
        return message, False

    # Convert the token budget back into a character budget (average 2.5 characters/token)
    char_budget = int(max_tokens * CHARS_PER_TOKEN)

    content = message.get("content", "")
    if isinstance(content, str):
        new_content = _truncate_head_tail(content, char_budget)
        if new_content == content:
            return message, False
        logger.warning(
            "[ContextManager] Layer C 压缩超大 user 消息: %d → %d 字符 (预算 %d tokens)",
            len(content), len(new_content), max_tokens,
        )
        return {**message, "content": new_content}, True

    if isinstance(content, list):
        # Find the largest text block and truncate it; keep the other blocks
        biggest_idx = -1
        biggest_len = 0
        for i, block in enumerate(content):
            if isinstance(block, dict) and str(block.get("type", "")).lower() in ("text", "output_text"):
                t = str(block.get("text", ""))
                if len(t) > biggest_len:
                    biggest_idx = i
                    biggest_len = len(t)
        if biggest_idx < 0 or biggest_len <= char_budget:
            return message, False
        new_blocks = list(content)
        target = new_blocks[biggest_idx]
        new_text = _truncate_head_tail(str(target.get("text", "")), char_budget)
        new_blocks[biggest_idx] = {**target, "text": new_text}
        logger.warning(
            "[ContextManager] Layer C 压缩超大 user 消息（多模态 block）: "
            "block %d %d → %d 字符 (预算 %d tokens)",
            biggest_idx, biggest_len, len(new_text), max_tokens,
        )
        return {**message, "content": new_blocks}, True

    return message, False


# ---------------------------------------------------------------------------
# Budget configuration
# ---------------------------------------------------------------------------


@dataclass
class ContextBudget:
    """Token budget allocation, with each partition managed independently.

    The total budget is allocated in this order:
    1. system_prompt_reserve: system prompt + skill descriptions
    2. memory_reserve: mem0 long-term memory injection
    3. output_reserve: reserved for model output
    4. tool_reserve: tool calls + results
    5. remaining space → history messages
    """
    model_context_window: int  # required: the model's real context window (no default fallback, see resolve_model_context_window)
    system_prompt_reserve: int = 10_000
    memory_reserve: int = 2_000
    output_reserve: int = 4_096
    tool_reserve: int = 20_000
    safety_margin: float = 0.10

    def __post_init__(self):
        reserved = (self.system_prompt_reserve + self.memory_reserve
                     + self.output_reserve + self.tool_reserve)
        if reserved >= self.model_context_window:
            logger.warning(
                "[ContextBudget] 预留空间 (%d) >= 模型上下文窗口 (%d)，历史消息预算为 0",
                reserved, self.model_context_window,
            )

    @property
    def history_budget(self) -> int:
        """Token count available for history messages."""
        used = (
            self.system_prompt_reserve
            + self.memory_reserve
            + self.output_reserve
            + self.tool_reserve
        )
        available = self.model_context_window - used
        return max(0, int(available * (1.0 - self.safety_margin)))

    @property
    def user_message_max_tokens(self) -> int:
        """Layer C: max token count a single user message may occupy."""
        return max(1_000, int(self.model_context_window * USER_MESSAGE_MAX_RATIO))


class ContextWindowManager:
    """Trims history messages before they are loaded into Agent Memory.

    Preserves user-assistant turn integrity: never keeps half a turn.
    """

    def __init__(self, budget: ContextBudget):
        self.budget = budget

    @classmethod
    def for_model(cls, model_name: str) -> "ContextWindowManager":
        """Automatically create a manager with the correct budget for the given model name."""
        ctx_window = resolve_model_context_window(model_name)
        budget = ContextBudget(model_context_window=ctx_window)
        return cls(budget=budget)

    def trim_history(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Keep messages from newest to oldest until the budget is exhausted.

        Preserves user-assistant turn integrity. Returns the trimmed message list.

        Key invariants:
        - **Always keep the last user message** (the current-turn request). Even if it
          alone exceeds the budget, it is force-kept — otherwise downstream would feed
          the current question into the summarizer too, and the model would end up with
          only the summary and not the actual question to answer. Layer C is responsible
          for truncating this message as needed so it doesn't blow up the model context.
        - Multimodal content (list of blocks) goes through ``estimate_message_tokens``,
          estimating per block type so base64 image data is never counted as text.
        - The cut point falls on a ``user`` boundary, keeping the OpenAI message sequence valid.
        """
        if not messages:
            return messages

        budget = max_tokens if max_tokens is not None else self.budget.history_budget

        # ── 1. Force-keep the latest user message (current-turn request) ──
        last_user_idx = len(messages) - 1
        while last_user_idx >= 0 and messages[last_user_idx].get("role") != "user":
            last_user_idx -= 1
        force_keep_from = last_user_idx if last_user_idx >= 0 else len(messages)

        # ── 2. Accumulate tokens back-to-front; cut at turn boundaries ──
        total_tokens = 0
        keep_from = len(messages)

        i = len(messages) - 1
        while i >= 0:
            msg = messages[i]
            tokens = estimate_message_tokens(msg.get("content", ""))
            # If i is in the "force-keep range" (>= last_user_idx), accept it regardless of budget
            if i >= force_keep_from:
                total_tokens += tokens
                keep_from = i
                i -= 1
                continue
            if total_tokens + tokens > budget:
                break
            total_tokens += tokens
            keep_from = i
            i -= 1

        # If the cut point lands mid-turn (an assistant/system message with no preceding user message),
        # skip forward until a user message is found as the start point; but never cross the force-keep point
        while (keep_from < force_keep_from
               and messages[keep_from].get("role") != "user"):
            keep_from += 1

        trimmed = messages[keep_from:]
        if len(trimmed) < len(messages):
            dropped = len(messages) - len(trimmed)
            logger.info(
                "[ContextManager] 裁剪历史消息: %d → %d 条 (丢弃 %d 条, 预算 %d tokens)",
                len(messages), len(trimmed), dropped, budget,
            )

        return trimmed

    def manage_context(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """One-shot pipeline running both defense layers, for the workflow layer to call.

        Execution order:
            1. Layer C  compress_oversized_user_message — compress the current user message if needed
            2. base     trim_history                    — trim against the token budget

        Returns:
            ``(kept_messages, dropped_messages)``: the kept messages (including the latest
            version possibly rewritten by Layer C) + the trimmed-off old messages, for the
            downstream summarizer to consume.
        """
        if not messages:
            return [], []

        working = list(messages)

        # Layer C: only the last user message gets the standalone check
        last_user_idx = len(working) - 1
        while last_user_idx >= 0 and working[last_user_idx].get("role") != "user":
            last_user_idx -= 1
        if last_user_idx >= 0:
            new_msg, did_compress = compress_oversized_user_message(
                working[last_user_idx],
                max_tokens=self.budget.user_message_max_tokens,
            )
            if did_compress:
                working[last_user_idx] = new_msg

        # Base trim
        kept = self.trim_history(working)
        dropped_count = len(working) - len(kept)
        dropped = working[:dropped_count] if dropped_count > 0 else []
        return kept, dropped

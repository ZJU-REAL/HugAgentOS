"""Default kind and truncation policy for each message role and block type."""
from __future__ import annotations
from typing import Any
from core.llm.context.ir_constants import KIND_ASSISTANT, KIND_ATTACHMENT, KIND_SYSTEM_RULE, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_USER_INPUT, POLICY_DROP, POLICY_HEAD_TAIL, POLICY_NEVER

def defaults(role: str, block_type: str) -> dict[str, Any]:
    if block_type == "thinking":
        # Reasoning is handed back to the model verbatim. It is counted on its
        # own so the usage panel can show what it costs, but it is never
        # shortened or dropped apart from the step that produced it.
        return {
            "kind": KIND_THINKING,
            "origin": "assistant:reasoning",
            "trust": "assistant",
            "priority": 400,
            "token_budget": 0,
            "truncation_policy": POLICY_NEVER,
            "render_role": "assistant",
        }
    if block_type == "tool_call":
        return {
            "kind": KIND_TOOL_CALL,
            "origin": "agent:tool_call",
            "trust": "assistant",
            "priority": 700,
            "token_budget": 0,
            "truncation_policy": POLICY_NEVER,
            "render_role": "assistant",
        }
    if block_type == "tool_result":
        return {
            "kind": KIND_TOOL_RESULT,
            "origin": "tool:result",
            "trust": "tool",
            "priority": 700,
            "token_budget": 20_000,
            "truncation_policy": POLICY_HEAD_TAIL,
            "render_role": "assistant",
        }
    if block_type == "data":
        return {
            "kind": KIND_ATTACHMENT,
            "origin": "user:attachment",
            "trust": "user",
            "priority": 650,
            "token_budget": 4_000,
            "truncation_policy": POLICY_DROP,
            "render_role": "user",
        }
    if role == "system":
        return {
            "kind": KIND_SYSTEM_RULE,
            "origin": "platform:system_prompt",
            "trust": "platform",
            "priority": 1_000,
            "token_budget": 100_000,
            "truncation_policy": POLICY_NEVER,
            "render_role": "system",
        }
    if role == "assistant":
        return {
            "kind": KIND_ASSISTANT,
            "origin": "assistant:history",
            "trust": "assistant",
            "priority": 400,
            "token_budget": 0,
            "truncation_policy": POLICY_NEVER,
            "render_role": "assistant",
        }
    return {
        "kind": KIND_USER_INPUT,
        "origin": "user:chat",
        "trust": "user",
        "priority": 500,
        "token_budget": 100_000,
        "truncation_policy": POLICY_HEAD_TAIL,
        "render_role": "user",
    }

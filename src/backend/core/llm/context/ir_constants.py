"""Context item kinds, policies and metadata keys."""
from __future__ import annotations


CONTEXT_SCHEMA_VERSION = "harness.context.v2"


SESSION_CONTEXT_META_KEY = "_context_item"


CONTEXT_SEQUENCE_STRIDE = 1_000


KIND_SYSTEM_RULE = "system_rule"


KIND_USER_INPUT = "user_input"


KIND_ASSISTANT = "assistant_history"


KIND_MEMORY = "memory"


KIND_IDENTITY = "identity"


KIND_PROJECT = "project_material"


KIND_THINKING = "thinking"


KIND_TOOL_CALL = "tool_call"


KIND_TOOL_RESULT = "tool_result"


KIND_REMINDER = "reminder"


KIND_COMPACTION = "compaction_summary"


KIND_ATTACHMENT = "attachment"


KIND_STEER = "steer"


KIND_REFERENCE = "reference"


POLICY_NEVER = "never"


POLICY_DROP = "drop"


POLICY_HEAD_TAIL = "head_tail"


POLICY_TAIL = "tail"


VISIBILITY_MODEL = "model"


VISIBILITY_MANIFEST_ONLY = "manifest_only"


TOOL_KINDS = {KIND_TOOL_CALL, KIND_TOOL_RESULT}


STEP_KINDS = frozenset({KIND_ASSISTANT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT})


CONVERSATION_KINDS = STEP_KINDS | {KIND_USER_INPUT}


TRUNCATABLE_BLOCK_TYPES = {"tool_result"}


# The only tool_result fields a provider formatter sends. Everything else on
# the block (metadata, state, timestamps) never reaches the model, so it must
# never spend the model's context budget either.
TOOL_RESULT_MODEL_FIELDS = ("type", "id", "name", "output")


PRUNED_OUTPUT_TEMPLATE = "[tool output pruned to fit the context budget: {tokens} tokens omitted]"

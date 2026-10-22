"""Request correlation only; no persistence or network on the execution path."""

import uuid
from contextvars import ContextVar

from core.harness.log_context import model_call_id


def context_headers(*, model=False):
    from core.infra.logging import chat_id_var, trace_id_var
    from core.models.usage import CURRENT_MODEL_USAGE
    from core.harness.log_context import current_message_id as _current_message_id

    values = {
        "x-observation-chat-id": chat_id_var.get(),
        "x-observation-trace-id": trace_id_var.get(),
        "x-observation-message-id": _current_message_id.get(),
    }
    context = CURRENT_MODEL_USAGE.get()
    if context:
        values["x-observation-run-id"] = context.run_id
    if model:
        call_id = uuid.uuid4().hex
        model_call_id.set(call_id)
        values["x-observation-call-id"] = call_id
    else:
        from core.harness.tool_effect_context import CURRENT_TOOL_EFFECT

        effect = CURRENT_TOOL_EFFECT.get()
        if effect:
            values["x-observation-call-id"] = effect.result_id
            values["x-observation-run-id"] = effect.run_id
    return {k: str(v)[:128] for k, v in values.items() if v}

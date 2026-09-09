"""Map an authenticated device operation into the cloud receipt namespace."""
import hashlib
import json
from sqlalchemy.exc import IntegrityError

class RemoteOperationConflict(ValueError):
    """The authenticated operation key is already bound to another request."""


RECEIPT_TOOLS = frozenset({
    "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task",
})


def bind_remote_effect(user_id, server_id, tool_name, operation_id, arguments):
    # Called only after capability authentication and current tool authorization.
    from core.db.engine import SessionLocal
    from core.db.models import RemoteToolEffect

    if not user_id or tool_name not in RECEIPT_TOOLS:
        raise ValueError("unsupported remote operation")
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise ValueError("invalid remote operation id")
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    effect_id = remote_effect_id(user_id, server_id, operation_id)
    with SessionLocal() as db:
        row = db.get(RemoteToolEffect, effect_id)
        if row is None:
            db.add(RemoteToolEffect(effect_id=effect_id, user_id=user_id,
                                   server_id=server_id, tool_name=tool_name, args_hash=digest))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(RemoteToolEffect, effect_id)
        if row is None or row.user_id != user_id or row.tool_name != tool_name or row.args_hash != digest:
            raise RemoteOperationConflict("remote operation already belongs to different arguments or tool")
    return effect_id


def remote_effect_id(user_id, server_id, operation_id):
    return hashlib.sha256(json.dumps(
        ["desktop-automation-v1", user_id, server_id, operation_id],
        separators=(",", ":")).encode()).hexdigest()


def remember_remote_call(effect, server_id, gateway_url, schema_hash):
    from core.db.engine import SessionLocal
    from core.db.models import ChatRun, RemoteToolEffect, ToolEffectLedger

    with SessionLocal() as db:
        intent = db.query(ToolEffectLedger).filter_by(effect_id=effect.effect_id, event_type="intent").first()
        if intent is None:
            raise ValueError("missing operation intent")
        run = db.get(ChatRun, effect.run_id)
        if run is None:
            raise ValueError("missing operation owner")
        row = db.get(RemoteToolEffect, effect.effect_id)
        if row is None:
            db.add(RemoteToolEffect(effect_id=effect.effect_id, user_id=run.user_id,
                                   server_id=server_id, tool_name=intent.tool_name,
                                   args_hash=intent.args_hash, gateway_url=gateway_url,
                                   schema_hash=schema_hash))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(RemoteToolEffect, effect.effect_id)
        if (row is None or row.user_id != run.user_id or row.server_id != server_id
                or row.tool_name != intent.tool_name or row.gateway_url != gateway_url):
            raise ValueError("remote operation binding changed")


def lookup_remote_receipt(user_id, server_id, tool_name, operation_id):
    from core.db.engine import SessionLocal
    from core.db.models import RemoteToolEffect, ToolEffectReceipt

    effect_id = remote_effect_id(user_id, server_id, operation_id)
    with SessionLocal() as db:
        binding = db.get(RemoteToolEffect, effect_id)
        if binding is None:
            return {"outcome": "not_applied"}
        if binding.user_id != user_id or binding.tool_name != tool_name or binding.gateway_url:
            return {"outcome": "unknown"}
        receipt = db.get(ToolEffectReceipt, effect_id)
        if receipt is None:
            # Replay with the SAME key remains protected by the atomic receipt,
            # even if the original call is still finishing.
            return {"outcome": "not_applied"}
        if receipt.user_id != user_id or receipt.tool_name != tool_name:
            return {"outcome": "unknown"}
        return {"outcome": "applied", "result": dict(receipt.result_payload or {})}

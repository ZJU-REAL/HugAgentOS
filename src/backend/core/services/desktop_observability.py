"""Cloud management projection. Delivery is idempotent and isolated by authenticated identity."""

from __future__ import annotations

import hashlib
import json
import math
import re

from core.db.models.observability import DesktopRecord, DesktopSyncDevice, utcnow
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

KINDS = {"session", "message", "run", "tool", "skill", "agent", "usage", "model"}
MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 512 * 1024
SECRET_KEY = re.compile(
    r"password|passwd|token|api.?key|authorization|cookie|secret|credential|access.?key", re.I
)
SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|\b(?:sk-[A-Za-z0-9_-]{8,}|dcap[12]\.[A-Za-z0-9._-]+)"
)
ASSIGNMENT = re.compile(
    r"""(?i)((?:password|api[_-]?key|access[_-]?key|secret|token|authorization)\s*[:=]\s*["']?)[^\s,"'}]+"""
)
# Only these business fields may leave a desktop. Never serialize ORM relationships or full metadata.
FIELDS = {
    "session": "chat_id title message_count created_at updated_at deleted_at",
    "message": "message_id chat_id chat_seq role content model tool_calls usage error created_at",
    "run": "run_id chat_id message_id status created_at updated_at started_at completed_at error_message usage agent_id agent_name",
    "tool": "id trace_id chat_id message_id tool_name tool_display_name tool_call_id mcp_server tool_args tool_result status source duration_ms error_message subagent_log_id skill_log_id started_at created_at effect_id",
    "skill": "id trace_id chat_id message_id skill_id skill_name skill_version skill_source invocation_type script_name script_language script_args script_stdout script_stderr output_truncated exit_code status source duration_ms error_message subagent_log_id started_at created_at",
    "agent": "id trace_id chat_id message_id subagent_id subagent_name subagent_type model input_messages output_content token_usage tool_calls_count skill_calls_count status error_message duration_ms parent_subagent_log_id started_at completed_at created_at",
    "usage": "chat_id attempt_id run_id kind operation_name provider model effect_id prompt_tokens completion_tokens cache_read_tokens cache_write_tokens latency_ms status retry_of attempt_seq created_at",
    "model": "chat_id run_id message_id attempt_id provider model status duration_ms usage error created_at",
}
EXTRA_FIELDS = {
    "run_id",
    "deleted",
    "truncated",
    "execution_location",
    "capability_origin",
    "call_id",
    "usage_known",
}


def key(*parts):
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()


def scrub(value, depth=0, truncated=None):
    truncated = truncated if truncated is not None else []
    if depth > 12:
        truncated.append(True)
        return "[truncated]"
    if isinstance(value, dict):
        if len(value) > 200:
            truncated.append(True)
        return {
            str(k)[:200]: (
                "[redacted]"
                if SECRET_KEY.search(str(k))
                and str(k)
                not in {
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "token_usage",
                    "prompt_tokens_details",
                    "completion_tokens_details",
                    "input_tokens_details",
                    "output_tokens_details",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                }
                else scrub(v, depth + 1, truncated)
            )
            for k, v in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            truncated.append(True)
        return [scrub(v, depth + 1, truncated) for v in value[:500]]
    if isinstance(value, str):
        if len(value) > 100000:
            truncated.append(True)
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
            "[redacted private key]",
            value,
            flags=re.S,
        )
        value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", value)
        value = re.sub(
            r"""(?i)(["'](?:api[_-]?key|password|secret|token|authorization)["']\s*:\s*["'])[^"']*""",
            r"\1[redacted]",
            value,
        )
        return ASSIGNMENT.sub(r"\1[redacted]", SECRET_TEXT.sub("[redacted]", value[:100000]))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(
        value.isoformat() if hasattr(value, "isoformat") else str(value), depth + 1, truncated
    )


def public_payload(kind, payload):
    fields = set(FIELDS[kind].split()) | EXTRA_FIELDS
    truncated = []
    result = scrub({k: v for k, v in payload.items() if k in fields}, truncated=truncated)
    while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_EVENT_BYTES - 4096:
        candidates = [
            k
            for k in result
            if k not in {"chat_id", "run_id", "message_id", "call_id", "truncated"}
        ]
        largest = max(candidates, key=lambda k: len(json.dumps(result[k], ensure_ascii=False)))
        result[largest] = str(result[largest])[:8192] + "\n[truncated]"
        truncated.append(True)
    if truncated:
        result["truncated"] = True
    return result


def insert_for(db, model):
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    return insert(model)


def ingest(db, user_id, device_id, batch, *, observation="desktop"):
    if not user_id or not device_id or len(device_id) > 128:
        raise ValueError("authenticated device required")
    events = batch.get("events")
    if not isinstance(events, list) or len(events) > 64:
        raise ValueError("invalid event batch")
    if len(json.dumps(batch, ensure_ascii=False).encode()) > MAX_BATCH_BYTES:
        raise ValueError("batch too large")
    normalized = []
    for e in events:
        if not isinstance(e, dict):
            raise ValueError("invalid event")
        kind, oid, rev = e.get("kind"), e.get("object_id"), e.get("revision")
        if kind not in KINDS or not isinstance(oid, str) or not 0 < len(oid) <= 128:
            raise ValueError("invalid event identity")
        if type(rev) is not int or not 0 < rev < 2**63 or not isinstance(e.get("payload"), dict):
            raise ValueError("invalid event revision or payload")
        payload = public_payload(kind, e["payload"])
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_EVENT_BYTES:
            raise ValueError("event too large")
        normalized.append((kind, oid, rev, payload))
    for kind, oid, rev, payload in normalized:
        if kind == "usage" and payload.get("kind") == "model":
            kind = "model"
            oid = str(payload.get("call_id") or oid)
            payload["usage"] = {
                k: payload.get(k)
                for k in (
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            }
            payload["usage_known"] = any(
                payload.get(k) for k in ("prompt_tokens", "completion_tokens")
            )
        values = dict(
            id=key(user_id, device_id, observation, kind, oid),
            user_id=user_id,
            device_id=device_id,
            kind=kind,
            object_id=oid,
            revision=rev,
            chat_id=str(payload.get("chat_id") or "")[:128],
            run_id=str(payload.get("run_id") or "")[:128],
            observation=observation,
            payload=payload,
            received_at=utcnow(),
        )
        stmt = insert_for(db, DesktopRecord).values(**values)
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={k: v for k, v in values.items() if k != "id"},
                where=DesktopRecord.revision < rev,
            )
        )
    if observation == "desktop":
        values = dict(
            id=key(user_id, device_id),
            user_id=user_id,
            device_id=device_id,
            last_seen=utcnow(),
            pending=max(0, min(int(batch.get("pending", 0)), 10**9)),
            capture_errors=max(0, min(int(batch.get("capture_errors", 0)), 10**9)),
            reconciling=bool(batch.get("reconciling", False)),
        )
        db.execute(
            insert_for(db, DesktopSyncDevice)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["id"], set_={k: v for k, v in values.items() if k != "id"}
            )
        )
    db.commit()
    return {
        "acknowledged": [{"kind": k, "object_id": o, "revision": r} for k, o, r, _ in normalized]
    }


def list_records(
    db, *, kind=None, user_id=None, device_id=None, chat_id=None, run_id=None, page=1, page_size=50
):
    gateway = aliased(DesktopRecord)
    matched_gateway = (
        select(gateway.id)
        .where(
            gateway.user_id == DesktopRecord.user_id,
            gateway.device_id == DesktopRecord.device_id,
            gateway.kind == DesktopRecord.kind,
            gateway.object_id == DesktopRecord.object_id,
            gateway.observation == "gateway",
        )
        .exists()
    )
    q = select(DesktopRecord).where(or_(DesktopRecord.observation == "gateway", ~matched_gateway))
    for column, value in (
        (DesktopRecord.kind, kind),
        (DesktopRecord.user_id, user_id),
        (DesktopRecord.device_id, device_id),
        (DesktopRecord.chat_id, chat_id),
        (DesktopRecord.run_id, run_id),
    ):
        if value is not None:
            q = (
                q.where(column.in_(["agent", "run"]))
                if column is DesktopRecord.kind and value == "agent"
                else q.where(column == value)
            )
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    ordering = (
        (DesktopRecord.payload["chat_seq"].as_integer(), DesktopRecord.id)
        if kind == "message" and chat_id
        else (DesktopRecord.received_at.desc(), DesktopRecord.id)
    )
    rows = db.scalars(q.order_by(*ordering).offset((page - 1) * page_size).limit(page_size)).all()
    output = []
    local_ids = [
        key(r.user_id, r.device_id, "desktop", r.kind, r.object_id)
        for r in rows
        if r.observation == "gateway"
    ]
    counterparts = (
        {
            r.id: r
            for r in db.scalars(select(DesktopRecord).where(DesktopRecord.id.in_(local_ids))).all()
        }
        if local_ids
        else {}
    )
    for row in rows:
        item = {c.name: getattr(row, c.name) for c in DesktopRecord.__table__.columns}
        local = counterparts.get(
            key(row.user_id, row.device_id, "desktop", row.kind, row.object_id)
        )
        if row.observation == "gateway" and local:
            payload = {**local.payload, **{k: v for k, v in row.payload.items() if v is not None}}
            if not row.payload.get("usage_known") and local.payload.get("usage"):
                payload["usage"] = local.payload["usage"]
                payload["usage_known"] = local.payload.get("usage_known", False)
            item["payload"] = payload
        output.append(item)
    return output, total

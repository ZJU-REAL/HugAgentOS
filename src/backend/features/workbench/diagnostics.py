"""Bounded, allowlisted diagnostics. No body, URL, credential or exception text."""

import hashlib
import re
import time
from collections import Counter
from uuid import uuid4

from core.db.models import ContentBlock
from core.infra.logging import chat_id_var, get_logger, trace_id_var, user_id_var
from core.infra.time import utc_now
from fastapi import HTTPException
from sqlalchemy import MetaData, Table, distinct, func, inspect, select
from sqlalchemy.orm import Session

PREFIX = "workbench_diagnostic:"
CODES = {
    "ok",
    "app_not_found",
    "app_disabled",
    "permission_denied",
    "host_incompatible",
    "origin_unavailable",
    "execution_scope_unsupported",
    "revision_conflict",
    "package_asset_missing",
    "bridge_timeout",
    "page_failed",
    "quota_denied",
    "release_failed",
    "launch_failed",
    "probe_failed",
    "origin_mismatch",
    "plugin_not_ready",
    "plugin_not_enabled",
    "plugin_source_unavailable",
    "plugin_package_missing",
    "plugin_ui_unavailable",
    "auth_configuration_required",
    "host_cookie_scope_overlap",
    "auth_intent_expired",
    "auth_intent_invalid",
    "launch_expired",
}
EVENTS = {"directory", "launch", "handshake", "resource", "release", "probe"}
logger = get_logger(__name__)


def opaque(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value else None


def revision(value):
    text = str(value)
    return text if re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", text) else opaque(text)


def code(value):
    return value if value in CODES else "launch_failed"


def record(db, user_id, event, outcome, item=None, instance_id=None, duration_ms=0):
    """Independent transaction never commits a caller's pending business changes."""
    item = item or {}
    binding = item.get("binding") or {}
    identity = uuid4().hex
    trace = trace_id_var.get()
    value = {
        "diagnosticId": identity,
        "traceId": trace if re.fullmatch(r"[a-fA-F0-9-]{16,64}", trace or "") else identity,
        "appKey": item.get("applicationKey"),
        "instanceId": opaque(instance_id),
        "installId": opaque(binding.get("installId")),
        "revision": revision(item.get("revision", "")),
        "scope": (
            binding.get("executionScope")
            if binding.get("executionScope") in {"cloud", "local"}
            else "cloud"
        ),
        "event": event if event in EVENTS else "launch",
        "code": code(outcome),
        "durationMs": max(0, min(int(duration_ms), 60000)),
        "timestamp": utc_now().isoformat(),
    }
    # Application keys only come from catalog projection, never raw client input.
    with Session(bind=db.get_bind()) as session:
        session.add(
            ContentBlock(
                id=PREFIX + identity,
                payload={"owner": opaque(user_id), **value},
                updated_at=utc_now(),
            )
        )
        stale = (
            session.query(ContentBlock.id)
            .filter(ContentBlock.id.startswith(PREFIX, autoescape=True))
            .order_by(ContentBlock.updated_at.desc(), ContentBlock.id.desc())
            .offset(1000)
            .limit(1000)
            .all()
        )
        if stale:
            session.query(ContentBlock).filter(ContentBlock.id.in_([r[0] for r in stale])).delete(
                synchronize_session=False
            )
        session.commit()
    trace_token = trace_id_var.set(value["traceId"])
    user_token = user_id_var.set(opaque(user_id) or "")
    chat_token = chat_id_var.set("")
    try:
        logger.info(
            "workbench_diagnostic",
            operation=value["event"],
            **{k: v for k, v in value.items() if k != "event"},
        )
    finally:
        chat_id_var.reset(chat_token)
        user_id_var.reset(user_token)
        trace_id_var.reset(trace_token)
    return value


def recent(db, owner=None, limit=100):
    query = db.query(ContentBlock).filter(ContentBlock.id.startswith(PREFIX, autoescape=True))
    if owner is not None:
        query = query.filter(ContentBlock.payload["owner"].as_string() == opaque(owner))
    return [
        {k: v for k, v in row.payload.items() if k != "owner"}
        for row in query.order_by(ContentBlock.updated_at.desc(), ContentBlock.id.desc())
        .limit(limit)
        .all()
    ]


def own(db, user_id, identity):
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise HTTPException(
            404, detail={"code": "diagnostic_not_found"}, headers={"Cache-Control": "no-store"}
        )
    row = db.get(ContentBlock, PREFIX + identity)
    if row is None or row.payload.get("owner") != opaque(user_id):
        raise HTTPException(
            404, detail={"code": "diagnostic_not_found"}, headers={"Cache-Control": "no-store"}
        )
    return {k: v for k, v in row.payload.items() if k != "owner"}


def metrics(db):
    rows = recent(db, limit=1000)
    counts = Counter((r["event"], r["code"]) for r in rows)
    columns = {c["name"] for c in inspect(db.get_bind()).get_columns("plugin_resources")}
    active = None
    if {"application_key", "instance_id"} <= columns:
        table = Table("plugin_resources", MetaData(), autoload_with=db.get_bind())
        active = db.scalar(
            select(func.count(distinct(table.c.instance_id))).where(
                table.c.application_key.is_not(None),
                table.c.status == "active",
                table.c.expires_at > time.time(),
            )
        )
    launches = [r for r in rows if r["event"] == "launch"]
    return {
        "window": "latest_1000_events",
        "activeInstances": active,
        "activeInstancesStatus": (
            "available" if active is not None else "awaiting_application_owner_schema"
        ),
        "launchSuccessRate": (
            sum(r["code"] == "ok" for r in launches) / len(launches) if launches else None
        ),
        "handshakeTimeouts": sum(r["code"] == "bridge_timeout" for r in rows),
        "permissionDenials": sum(
            r["code"] in {"permission_denied", "plugin_not_enabled", "plugin_not_ready"}
            for r in rows
        ),
        "quotaDenials": sum(r["code"] == "quota_denied" for r in rows),
        "releaseFailures": sum(r["code"] == "release_failed" for r in rows),
        "events": [
            {"event": event, "code": outcome, "count": n}
            for (event, outcome), n in sorted(counts.items())
        ],
        "directoryDurationMs": [r["durationMs"] for r in rows if r["event"] == "directory"],
    }


def optional_record(db, user_id, event, outcome, **context):
    """Telemetry has its own transaction and cannot change a business outcome."""
    try:
        return record(db, user_id, event, outcome, **context)
    except Exception:
        # Neither database/exception details nor logging failures cross this boundary.
        try:
            logger.warning("workbench_diagnostic_unavailable", operation=event)
        except Exception:
            pass
        return None


def observed(event):
    """Instrument directory/launch without replaying effects or requiring telemetry."""
    from functools import wraps
    from time import perf_counter

    def decorate(handler):
        @wraps(handler)
        def run(*args, **kwargs):
            started = perf_counter()
            db, user = kwargs.get("db"), kwargs.get("user")
            item = None
            if event == "launch":
                from features.workbench import catalog

                try:
                    item = catalog.readable(db, user.user_id, kwargs["key"])
                except HTTPException:
                    pass
            try:
                result = handler(*args, **kwargs)
            except HTTPException as error:
                db.rollback()
                detail = error.detail if isinstance(error.detail, dict) else {"code": error.detail}
                value = optional_record(
                    db, user.user_id, event, code(detail.get("code")), item=item,
                    duration_ms=(perf_counter() - started) * 1000,
                )
                headers = {"Cache-Control": "no-store", **(error.headers or {})}
                if value is None:
                    error.headers = headers
                    raise
                raise HTTPException(
                    error.status_code,
                    detail={"code": code(detail.get("code")), "diagnosticId": value["diagnosticId"]},
                    headers=headers,
                ) from None
            except Exception:
                db.rollback()
                # Unexpected business failures remain failures; never expose their details.
                value = optional_record(
                    db, user.user_id, event, "launch_failed", item=item,
                    duration_ms=(perf_counter() - started) * 1000,
                )
                detail = {"code": "launch_failed"}
                if value is not None:
                    detail["diagnosticId"] = value["diagnosticId"]
                raise HTTPException(
                    500, detail=detail, headers={"Cache-Control": "no-store"},
                ) from None
            optional_record(
                db, user.user_id, event, "ok", item=item,
                instance_id=(
                    ((result.get("data") or {}).get("context") or {}).get("instanceId")
                    if isinstance(result, dict) else None
                ),
                duration_ms=(perf_counter() - started) * 1000,
            )
            return result

        return run

    return decorate


def record_resource(db, resource, event, outcome):
    """Observe a server-resolved application-owned resource, never a plugin ID claim."""
    key = getattr(resource, "application_key", None)
    if not key:
        return None
    return record(
        db,
        resource.user_id,
        event,
        outcome,
        item={
            "applicationKey": key,
            "revision": resource.revision,
            "binding": {"installId": resource.install_id, "executionScope": resource.scope},
        },
        instance_id=resource.instance_id,
    )


def observe_resource(db, resource, event, outcome):
    """Optional telemetry must not change actual worker admission or teardown outcomes."""
    try:
        return record_resource(db, resource, event, outcome)
    except Exception:
        logger.warning("workbench_resource_diagnostic_unavailable", operation=event)
        return None

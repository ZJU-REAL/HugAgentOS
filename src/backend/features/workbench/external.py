"""Ordinary external application definitions. ContentBlock is the single durable source.

Legacy app_config entries stay in their existing source until WB-20 migration.
New entries cannot claim legacy or builtin IDs. No external authentication secrets
are accepted. Audit metadata and each CAS update commit together.
"""

import ipaddress
import re
from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from features.workbench.coordination import configuration
from features.workbench.protocol import validate_application
from core.files.content.branding import normalize_app_config
from core.db.models import ContentBlock
from core.infra.time import utc_now
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

PREFIX = "workbench_external:"
AUDIT = "workbench_audit:"


def fail(status, code):
    raise HTTPException(status_code=status, detail={"code": code})


def legacy_ids(db):
    from features.workbench.migration_compat import app_config

    return {app["id"] for app in app_config(db)["apps"]} | {
        "plan_mode",
        "automation",
    }


def validate_url(value):
    try:
        if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
            raise ValueError()
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
        ):
            raise ValueError()
        _ = url.port
        host = url.hostname.encode("idna").decode("ascii")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # WHATWG browsers interpret decimal/octal/hex numeric hosts as IPv4.
            # Require canonical IP syntax instead of accepting these as DNS names.
            last_label = host.rstrip(".").split(".")[-1]
            if re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", last_label):
                raise ValueError()
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", p)
                for p in host.rstrip(".").split(".")
            ):
                raise ValueError()
        if any(
            k.lower() in {"token", "sso_token", "access_token", "authorization", "password"}
            for k, _ in parse_qsl(url.query)
        ):
            raise ValueError()
    except (ValueError, UnicodeError):
        fail(422, "invalid_external_url")


def validate_definition(db, declaration):
    issues = validate_application(declaration)
    if issues:
        raise HTTPException(422, detail={"code": "invalid_application", "issues": issues})
    if set(declaration) - {"schemaVersion", "id", "title", "description", "target"}:
        fail(422, "unsupported_external_field")
    target = declaration["target"]
    if target["kind"] != "external" or target["mode"] not in ("browser", "embedded"):
        fail(422, "unsupported_external_target")
    validate_url(target["url"])
    if declaration["id"] in legacy_ids(db):
        fail(409, "legacy_application_id")


def items(db):
    rows = (
        db.query(ContentBlock)
        .filter(ContentBlock.id.startswith(PREFIX, autoescape=True))
        .order_by(ContentBlock.id)
        .all()
    )
    return sorted(
        [deepcopy(row.payload) for row in rows],
        key=lambda item: (bool(item.get("legacy")), item.get("legacyOrder", 0)),
    )


def record(db, key):
    if not key.startswith("external:"):
        fail(404, "app_not_found")
    row = db.get(ContentBlock, PREFIX + key[len("external:") :])
    if row is None:
        fail(404, "app_not_found")
    return deepcopy(row.payload)


def save(db, declaration, enabled, actor, trace, revision=None, key=None, embedding=None):
    configuration(db)
    validate_definition(db, declaration)
    from features.workbench.external_embed import validate_policy

    embedding = validate_policy(declaration["target"], embedding)
    expected_key = "external:" + declaration["id"]
    if key is not None and key != expected_key:
        fail(422, "immutable_application_id")
    value = {
        "applicationKey": expected_key,
        "declaration": deepcopy(declaration),
        "enabled": enabled,
        "revision": 1 if revision is None else revision + 1,
    }
    if embedding is not None:
        value["embedding"] = embedding
    row_id = PREFIX + declaration["id"]
    now = utc_now()
    if revision is None:
        db.add(ContentBlock(id=row_id, payload=value, updated_by=actor, updated_at=now))
    else:
        count = db.execute(
            update(ContentBlock)
            .where(
                ContentBlock.id == row_id,
                ContentBlock.payload["revision"].as_integer() == revision,
            )
            .values(payload=value, updated_by=actor, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if count != 1:
            db.rollback()
            fail(409, "revision_conflict")
    db.add(
        ContentBlock(
            id=AUDIT + uuid4().hex,
            payload={
                "applicationKey": expected_key,
                "revision": value["revision"],
                "enabled": enabled,
                "action": "created" if revision is None else "updated",
                "actor": actor,
                "trace_id": trace,
                "timestamp": now.isoformat(),
            },
            updated_by=actor,
            updated_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "application_exists")
    db.expire_all()
    return value


def reject_legacy_conflicts(db, payload):
    from features.workbench.migration_compat import active

    if active(db):
        return  # The versioned adapter validates and writes canonical records.
    configuration(db)
    ids = {a["id"] for a in normalize_app_config(payload)["apps"]}
    if ids & {item["declaration"]["id"] for item in items(db)}:
        fail(409, "workbench_application_id")

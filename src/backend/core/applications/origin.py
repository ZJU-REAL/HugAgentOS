"""Persistent, public service identity; never derived from URLs or private paths."""

import re
from uuid import uuid4

from core.config import local_mode
from core.db.models import ContentBlock
from core.infra.time import utc_now
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

IDENTITY = "workbench_origin_identity"


def current(db):
    row = db.get(ContentBlock, IDENTITY)
    if row is None:
        insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}[db.get_bind().dialect.name]
        db.execute(
            insert(ContentBlock)
            .values(id=IDENTITY, payload={"id": uuid4().hex}, updated_at=utc_now())
            .on_conflict_do_nothing(index_elements=[ContentBlock.id])
        )
        db.commit()
        row = db.get(ContentBlock, IDENTITY)
    identifier = row.payload.get("id") if isinstance(row.payload, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise HTTPException(409, detail={"code": "origin_identity_invalid"})
    return {"kind": "local" if local_mode.local_mode_enabled() else "cloud", "id": identifier}


def require_current(request, db):
    values = [
        value
        for value in (
            request.headers.get("x-workbench-origin"),
            request.query_params.get("hg_origin"),
        )
        if value is not None
    ]
    if values and any(value != current(db)["id"] for value in values):
        raise HTTPException(409, detail={"code": "origin_mismatch"})


def execution_reason(declaration, source):
    scopes = declaration.get("availability", {}).get("executionScopes")
    if scopes is not None and source["kind"] not in scopes:
        return "execution_scope_unsupported"
    return None

"""Resolve the exact set of file roots visible to one authenticated account."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re

from fastapi import HTTPException
from core.files.storage.entries import validate_key

def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise HTTPException(400, "Invalid space identity")
    return value

@dataclass(frozen=True)
class SpaceGrant:
    space_id: str
    name: str
    kind: str
    owner_id: str
    permission: str
    prefix: str
    mount_path: str

    @property
    def writable(self):
        return self.permission in ("edit", "admin")

    def key(self, path: str, *, directory=False):
        try:
            return self.prefix + validate_key(path, directory=directory)
        except ValueError as exc:
            raise HTTPException(400, "Invalid relative file path") from exc

    def public(self):
        return {k: v for k, v in asdict(self).items() if k not in ("prefix", "owner_id")}

def namespace(kind: str, owner: str) -> str:
    environment = identifier(os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")))
    return f"{environment}/spaces/{kind}/{identifier(owner)}/files/"

def personal_grant(actor):
    actor = identifier(actor)
    return SpaceGrant("personal", "我的文件", "personal", actor, "admin",
        namespace("users", actor), f"/workspace/myspace/{actor}")

def resolve_grants(db, actor: str) -> tuple[SpaceGrant, ...]:
    actor = identifier(actor)
    if actor.startswith("api_ak_"):
        return ()
    from core.files.edition import team_grants
    return (personal_grant(actor), *team_grants(db, actor))

def require_grant(db, actor: str, space_id: str, *, write=False) -> SpaceGrant:
    grant = next((g for g in resolve_grants(db, actor) if g.space_id == space_id), None)
    if grant is None:
        raise HTTPException(404, "Space is not available")
    if write and not grant.writable:
        raise HTTPException(403, "Space is read-only")
    return grant

def fingerprint(grants) -> str:
    payload = json.dumps([asdict(g) for g in grants], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()

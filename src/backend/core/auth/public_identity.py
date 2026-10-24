"""Resolve a visitor credential without granting the owner's account identity."""
import hashlib
import re
from fastapi import HTTPException
from core.db.models import ContentBlock, UserApiKey
from core.infra.crypto import decrypt_secret
from core.auth.api_keys import resolve_api_key_identity

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def resolve_public_user(db, token, visitor):
    from core.auth.backend import UserContext

    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,64}", visitor
    ):
        raise HTTPException(401, detail="Invalid public chat link")
    row = db.get(ContentBlock, "publicchat:token:" + digest(token))
    key = db.get(UserApiKey, row.payload["key_id"]) if row else None
    identity = (
        resolve_api_key_identity(db, decrypt_secret(key.key_enc)) if key and key.key_enc else None
    )
    if not identity:
        raise HTTPException(401, detail="Public chat link is unavailable")
    owner, key = identity
    return UserContext(
        user_id=owner.user_id,
        user_center_id=owner.user_center_id or "",
        username=owner.username,
        api_key_id=key.id,
        api_key_agent_id=key.agent_id,
        api_key_project_id=key.project_id,
        public_chat_visitor=digest(visitor),
    )

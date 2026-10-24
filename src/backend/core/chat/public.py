"""Public entry links reuse scoped API credentials; visitor scope never grants account access."""

import secrets
from core.auth.public_identity import digest
import uuid
from datetime import datetime, timezone

from core.db.models import ContentBlock, UserApiKey, UserShadow
from core.infra.crypto import decrypt_secret, encrypt_secret
from core.auth.api_keys import ApiKeyService
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError




def require_manager(db, user_id, kind, resource_id):
    from core.auth.capabilities import resolve_user_capabilities
    from core.auth.agent_api import require_agent_manager
    from core.projects.api import require_project_key_access

    if kind not in ("agents", "projects"):
        raise HTTPException(404, detail="Unknown resource")
    if not resolve_user_capabilities(db, user_id)["can_use_api_key"]:
        raise HTTPException(403, detail="API access is disabled")
    checker = require_agent_manager if kind == "agents" else require_project_key_access
    return checker(db, user_id, resource_id)


def link_id(user_id, kind, resource_id):
    return "publicchat:resource:" + digest(f"{user_id}:{kind}:{resource_id}")


def get_link(db, user_id, kind, resource_id):
    require_manager(db, user_id, kind, resource_id)
    row = db.get(ContentBlock, link_id(user_id, kind, resource_id))
    if not row:
        return None
    key = db.get(UserApiKey, row.payload["key_id"])
    if not key or key.revoked_at is not None:
        return None
    return {"token": decrypt_secret(row.payload["token_enc"]), "enabled": key.enabled}


def create_link(db, user_id, kind, resource_id):
    require_manager(db, user_id, kind, resource_id)
    # Lock a stable row before checking/creating the singleton link. SQLite's unique
    # ContentBlock PK arbitrates concurrent inserts; Postgres also serializes here.
    db.query(UserShadow).filter(UserShadow.user_id == user_id).with_for_update().first()
    existing = get_link(db, user_id, kind, resource_id)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    raw = "sk-jx-" + secrets.token_urlsafe(32)
    key = UserApiKey(
        id="ak_" + uuid.uuid4().hex[:20],
        user_id=user_id,
        name="Public chat",
        key_hash=digest(raw),
        key_prefix=raw[:14],
        key_enc=encrypt_secret(raw),
        enabled=True,
        created_at=datetime.now(timezone.utc),
        **({"agent_id": resource_id} if kind == "agents" else {"project_id": resource_id}),
    )
    payload = {"key_id": key.id, "kind": kind, "resource_id": resource_id}
    resource = db.get(ContentBlock, link_id(user_id, kind, resource_id))
    try:
        with db.begin_nested():
            db.add(key)
            if resource:
                resource.payload = {**payload, "token_enc": encrypt_secret(token)}
            else:
                db.add(
                    ContentBlock(
                        id=link_id(user_id, kind, resource_id),
                        payload={**payload, "token_enc": encrypt_secret(token)},
                        updated_by=user_id,
                    )
                )
            db.add(
                ContentBlock(
                    id="publicchat:token:" + digest(token), payload=payload, updated_by=user_id
                )
            )
            db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = get_link(db, user_id, kind, resource_id)
        if winner:
            return winner
        raise
    return {"token": token, "enabled": True}


def set_enabled(db, user_id, kind, resource_id, enabled):
    require_manager(db, user_id, kind, resource_id)
    row = db.get(ContentBlock, link_id(user_id, kind, resource_id))
    if not row:
        raise HTTPException(404, detail="Link not found")
    key = ApiKeyService(db).set_enabled(
        user_id,
        row.payload["key_id"],
        enabled,
        **({"agent_id": resource_id} if kind == "agents" else {"project_id": resource_id}),
    )
    if not key:
        raise HTTPException(404, detail="Link not found")
    return get_link(db, user_id, kind, resource_id)




def visitor_preference_id(user):
    return "publicchat:preferences:" + digest(f"{user.api_key_id}:{user.public_chat_visitor}")


def visitor_approval_mode(db, user, default=None):
    from core.sandbox.permissions.approval import normalize_approval_mode

    row = db.get(ContentBlock, visitor_preference_id(user))
    return normalize_approval_mode((row.payload if row else {}).get("tool_approval_mode", default))


def set_visitor_approval_mode(db, user, mode):
    from core.sandbox.permissions.approval import normalize_approval_mode

    key = visitor_preference_id(user)
    row = db.get(ContentBlock, key)
    payload = {"tool_approval_mode": normalize_approval_mode(mode)}
    if row:
        row.payload = {**row.payload, **payload}
    else:
        db.add(ContentBlock(id=key, payload=payload, updated_by=user.user_id))
    db.commit()


def prepare_visitor_upload(db, user, chat_id, folder_id=None):
    from core.chat.schemas import ChatRequest
    from core.auth.agent_api import prepare_agent_api_request
    from core.projects.api import prepare_project_api_request

    if not chat_id or folder_id:
        raise HTTPException(422, detail="A public upload requires its conversation")
    body = ChatRequest(chat_id=chat_id, message="Upload", agent_id=user.api_key_agent_id,
                       project_id=user.api_key_project_id)
    prepare_project_api_request(db, user, body)
    prepare_agent_api_request(db, user, body)
    return {"public_chat_visitor": user.public_chat_visitor, "public_chat_key_id": user.api_key_id,
            "agent_api_key_id": user.api_key_id}


def validate_visitor_attachments(db, user, attachments):
    from core.auth.public_chat import public_session_matches
    from core.db.models import Artifact, ChatSession

    for item in attachments:
        artifact = db.get(Artifact, item.file_id)
        session = db.get(ChatSession, artifact.chat_id) if artifact and artifact.chat_id else None
        if not artifact or artifact.deleted_at is not None or not public_session_matches(session, user):
            raise HTTPException(403, detail="Attachment is not in this visitor's conversations")

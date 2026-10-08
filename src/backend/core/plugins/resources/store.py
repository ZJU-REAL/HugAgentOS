"""Durable resource descriptors and single-use attachment tickets."""
import hashlib
import json
import secrets
import time
from core.db.models.plugin_resource import PluginResource, PluginResourceTicket, PluginResourceCheckpoint, PluginResourceAssetTicket
from .crypto import encrypt_secret, decrypt_secret

def public(row):
    return {"resource_id": row.resource_id, "chat_id": row.chat_id, "slug": row.slug, "module_id": row.module_id, "install_id": row.install_id, "revision": row.revision, "execution_scope": row.scope, "status": row.status}

def get(db, resource_id, user_id=None):
    row = db.get(PluginResource, resource_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        from fastapi import HTTPException
        raise HTTPException(404, "resource_unavailable")
    return row

def descriptor(row):
    value = decrypt_secret(row.descriptor)
    if value is None:
        from fastapi import HTTPException
        raise HTTPException(410, "resource_credentials_expired")
    return json.loads(value)

def ticket(db, row, origin, authentication=None):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    db.query(PluginResourceTicket).filter(PluginResourceTicket.expires_at < time.time()).delete()
    db.add(PluginResourceTicket(digest=digest, resource_id=row.resource_id, user_id=row.user_id, origin=origin, auth_enc=encrypt_secret(json.dumps(authentication)) if authentication is not None else "", expires_at=time.time() + 30))
    db.commit()
    return token

def consume(db, token, resource_id, origin, *, with_authentication=False):
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = db.get(PluginResourceTicket, digest)
    if row is None or row.resource_id != resource_id or row.origin != origin or row.expires_at < time.time():
        return None
    user_id = row.user_id
    decoded = decrypt_secret(row.auth_enc) if row.auth_enc else "{}"
    if decoded is None:
        return None
    authentication = json.loads(decoded)
    deleted = db.query(PluginResourceTicket).filter(PluginResourceTicket.digest == digest).delete()
    db.commit()
    if deleted != 1:
        return None
    return (user_id, authentication) if with_authentication else user_id

def asset_ticket(db, row, authentication=None):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    db.query(PluginResourceAssetTicket).filter(PluginResourceAssetTicket.expires_at < time.time()).delete()
    db.add(PluginResourceAssetTicket(digest=digest, resource_id=row.resource_id, user_id=row.user_id, expires_at=min(row.expires_at, time.time() + 300), auth_enc=encrypt_secret(json.dumps(authentication or {}))))
    db.commit()
    return token

def resolve_asset_ticket(db, token):
    row = db.get(PluginResourceAssetTicket, hashlib.sha256(token.encode()).hexdigest())
    if row is None or row.expires_at < time.time():
        from fastapi import HTTPException
        raise HTTPException(404, "asset_ticket_unavailable")
    context = decrypt_secret(row.auth_enc) if row.auth_enc else None
    if context is None:
        from fastapi import HTTPException
        raise HTTPException(404, "asset_ticket_unavailable")
    return row.resource_id, row.user_id, json.loads(context)

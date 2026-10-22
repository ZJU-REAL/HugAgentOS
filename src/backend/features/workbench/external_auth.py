"""External login configuration. Target systems own their IdP and sessions.

No host cookies or reusable host tokens cross this boundary. Client credentials
are high-entropy API secrets stored as hashes, never returned by read operations.
ContentBlock provides portable transactions without adding a second app source.
"""

from features.workbench.coordination import configuration


import hashlib
import hmac
import secrets
import time
from copy import deepcopy
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

from features.workbench import catalog, external
from core.db.models import ContentBlock
from core.infra.time import utc_now
from sqlalchemy import delete

CONFIG = "workbench_external_auth:"
TICKET = "workbench_external_ticket:"
CHALLENGE = "workbench_external_challenge:"
INTENT = "workbench_external_intent:"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def row(db, key):
    return db.get(ContentBlock, CONFIG + key)


def read(db, key):
    record = row(db, key)
    if not record:
        return {
            "revision": 0,
            "mode": "independent",
            "requiresConfiguration": key.startswith("legacy:"),
        }
    return {k: deepcopy(v) for k, v in record.payload.items() if k != "secretHash"}


def definition(db, key):
    for item in catalog.definitions(db):
        if item["applicationKey"] == key and item["declaration"]["target"]["kind"] == "external":
            return item
    external.fail(404, "app_not_found")


def origin(url):
    parts = urlsplit(url)
    return (
        parts.scheme.lower(),
        parts.hostname.lower(),
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def destination(value, base):
    external.validate_url(value)
    if urlsplit(value).fragment or origin(value) != origin(base):
        external.fail(422, "invalid_auth_destination")


def save(db, key, body, actor, trace):
    configuration(db)
    item = definition(db, key)
    target = item["declaration"]["target"]["url"]
    external.validate_url(target)
    record = row(db, key)
    current = record.payload if record else {"revision": 0}
    if body["revision"] != current["revision"]:
        db.rollback()
        external.fail(409, "revision_conflict")
    mode = body["mode"]
    value = {"revision": current["revision"] + 1, "mode": mode}
    if mode == "shared_identity":
        destination(body["loginUrl"], target)
        if not body["identityProvider"]:
            external.fail(422, "identity_provider_required")
        value.update(loginUrl=body["loginUrl"], identityProvider=body["identityProvider"])
    elif mode == "exchange":
        destination(body["callback"], target)
        destination(body["initiationUrl"], target)
        if not body["audience"]:
            external.fail(422, "audience_required")
        secret = body.get("clientSecret")
        secret_hash = digest(secret) if secret else current.get("secretHash")
        if not secret_hash:
            external.fail(422, "client_secret_required")
        value.update(
            callback=body["callback"],
            initiationUrl=body["initiationUrl"],
            audience=body["audience"],
            ttlSeconds=body["ttlSeconds"],
            secretHash=secret_hash,
        )
    if record:
        record.payload = value
        record.updated_at = utc_now()
        record.updated_by = actor
    else:
        db.add(ContentBlock(id=CONFIG + key, payload=value, updated_by=actor))
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(CHALLENGE, autoescape=True),
            ContentBlock.payload["applicationKey"].as_string() == key,
        )
    )
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(INTENT, autoescape=True),
            ContentBlock.payload["applicationKey"].as_string() == key,
        )
    )
    # Revocation/rotation invalidates outstanding launches, even if the app's
    # declaration revision has not changed.
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(TICKET, autoescape=True),
            ContentBlock.payload["applicationKey"].as_string() == key,
        )
    )
    db.add(
        ContentBlock(
            id=external.AUDIT + uuid4().hex,
            updated_by=actor,
            payload={
                "applicationKey": key,
                "action": "auth_updated",
                "actor": actor,
                "revision": value["revision"],
                "trace_id": trace,
            },
        )
    )
    db.commit()
    db.expire_all()
    return read(db, key)


def launch(db, user_id, item, session_hash=None):
    key = item["applicationKey"]
    target = deepcopy(item["declaration"]["target"])
    if target["kind"] != "external":
        return target
    external.validate_url(target["url"])
    config = read(db, key)
    if config["mode"] == "independent":
        if key.startswith("legacy:") and config["revision"] == 0:
            external.fail(409, "auth_configuration_required")
        return target
    if config["mode"] == "shared_identity":
        destination(config["loginUrl"], target["url"])
        target["url"] = config["loginUrl"]
        return target
    if key.startswith("legacy:") and config["revision"] == 0:
        external.fail(409, "auth_configuration_required")
    destination(config["initiationUrl"], target["url"])
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(INTENT, autoescape=True),
            ContentBlock.payload["expiresAt"].as_integer() <= now,
        )
    )
    db.add(
        ContentBlock(
            id=INTENT + digest(token),
            payload={
                "applicationKey": key,
                "subject": user_id,
                "sessionHash": session_hash,
                "appRevision": item["revision"],
                "authRevision": config["revision"],
                "expiresAt": now + 30,
            },
        )
    )
    db.commit()
    parts = urlsplit(config["initiationUrl"])
    target["url"] = urlunsplit((*parts[:4], urlencode({"intent": token})))
    return target


def target_config(db, key, credential):
    config_row = row(db, key)
    config = config_row.payload if config_row else {}
    expected = config.get("secretHash", "")
    if (
        config.get("mode") != "exchange"
        or not credential
        or not hmac.compare_digest(digest(credential), expected)
    ):
        db.rollback()
        external.fail(401, "invalid_target_credentials")
    return config


def challenge(db, body, credential):
    configuration(db)
    key = body["applicationKey"]
    config = target_config(db, key, credential)
    if body["audience"] != config["audience"] or body["callback"] != config["callback"]:
        external.fail(401, "invalid_target_binding")
    intent = db.execute(
        delete(ContentBlock)
        .where(ContentBlock.id == INTENT + digest(body["intent"]))
        .returning(ContentBlock.payload)
    ).scalar_one_or_none()
    if (
        not intent
        or intent["expiresAt"] <= int(time.time())
        or intent["applicationKey"] != key
        or intent["authRevision"] != config["revision"]
    ):
        db.commit()
        external.fail(401, "invalid_launch_intent")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(CHALLENGE, autoescape=True),
            ContentBlock.payload["expiresAt"].as_integer() <= now,
        )
    )
    db.add(
        ContentBlock(
            id=CHALLENGE + digest(token),
            payload={
                "applicationKey": key,
                "stateHash": digest(body["state"]),
                "authRevision": config["revision"],
                "expiresAt": now + 30,
                "subject": intent["subject"],
                "sessionHash": intent["sessionHash"],
                "appRevision": intent["appRevision"],
            },
        )
    )
    db.commit()
    return token


def authorize(db, user_id, body, session_hash=None, validate_boundary=None):
    configuration(db)
    # The authenticated target registered the browser nonce before the host
    # could issue a ticket. The target MUST also verify its own pre-login cookie.
    pending = db.execute(
        delete(ContentBlock)
        .where(ContentBlock.id == CHALLENGE + digest(body["challenge"]))
        .returning(ContentBlock.payload)
    ).scalar_one_or_none()
    if (
        not pending
        or pending["expiresAt"] <= int(time.time())
        or not hmac.compare_digest(pending["stateHash"], digest(body["state"]))
    ):
        db.commit()
        external.fail(401, "invalid_login_challenge")
    if pending["subject"] != user_id or (
        pending.get("sessionHash") and pending["sessionHash"] != session_hash
    ):
        db.commit()
        external.fail(409, "launch_identity_mismatch")
    key = pending["applicationKey"]
    item = catalog.readable(db, user_id, key)
    config = read(db, key)
    if (
        not item["availability"]["available"]
        or config["mode"] != "exchange"
        or config["revision"] != pending["authRevision"]
        or item["revision"] != pending["appRevision"]
    ):
        db.commit()
        external.fail(401, "invalid_login_challenge")
    destination(config["callback"], item["declaration"]["target"]["url"])
    if validate_boundary:
        validate_boundary(item["declaration"]["target"], config)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute(
        delete(ContentBlock).where(
            ContentBlock.id.startswith(TICKET, autoescape=True),
            ContentBlock.payload["expiresAt"].as_integer() <= now,
        )
    )
    db.add(
        ContentBlock(
            id=TICKET + digest(token),
            payload={
                "applicationKey": key,
                "subject": user_id,
                "audience": config["audience"],
                "callback": config["callback"],
                "stateHash": digest(body["state"]),
                "expiresAt": now + config["ttlSeconds"],
                "authRevision": config["revision"],
                "appRevision": item["revision"],
                "sessionHash": session_hash,
            },
        )
    )
    db.commit()
    parts = urlsplit(config["callback"])
    return urlunsplit((*parts[:4], urlencode({"ticket": token, "state": body["state"]})))


def redeem(db, body, credential):
    configuration(db)
    config = target_config(db, body["applicationKey"], credential)
    # DELETE RETURNING is one atomic statement on SQLite and PostgreSQL. No
    # fallback to SELECT then DELETE is safe when workers redeem concurrently.
    payload = db.execute(
        delete(ContentBlock)
        .where(ContentBlock.id == TICKET + digest(body["ticket"]))
        .returning(ContentBlock.payload)
    ).scalar_one_or_none()
    db.commit()
    if payload is None:
        external.fail(401, "invalid_launch_ticket")
    valid = (
        payload["expiresAt"] > int(time.time())
        and payload["applicationKey"] == body["applicationKey"]
        and payload["audience"] == body["audience"] == config["audience"]
        and payload["callback"] == body["callback"] == config["callback"]
        and payload["authRevision"] == config["revision"]
        and hmac.compare_digest(payload["stateHash"], digest(body["state"]))
    )
    if not valid:
        external.fail(401, "invalid_launch_ticket")
    # Effective current user grants and app state are rechecked at redemption.
    item = catalog.readable(db, payload["subject"], payload["applicationKey"])
    if not item["availability"]["available"] or item["revision"] != payload["appRevision"]:
        external.fail(401, "invalid_launch_ticket")
    return {
        "subject": payload["subject"],
        "audience": payload["audience"],
        "applicationKey": payload["applicationKey"],
        "_sessionHash": payload.get("sessionHash"),
    }

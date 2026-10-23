"""Real personal API-Key authentication and revocation on the isolated backend."""

import uuid
from core.db.engine import SessionLocal
from core.db.models import UserShadow
from core.auth.api_keys import ApiKeyService
from hugagent_cli.transport import Client

uid = "cli-key-test-" + uuid.uuid4().hex
with SessionLocal() as db:
    db.add(
        UserShadow(
            user_id=uid,
            user_center_id=uid,
            username="CLI key fixture",
            extra_data={"can_use_api_key": True},
        )
    )
    db.commit()
    row, key = ApiKeyService(db).create_key(uid, "Session CLI validation", expires_in_days=1)
    key_id = row.id
client = Client("http://127.0.0.1:3001", key)
try:
    assert client.request("GET", "/v1/me")["user_id"] == uid
    op = client.request(
        "POST",
        "/v1/personal-agent/session-manager/operations",
        {
            "target": "cloud",
            "operation": "chat.create",
            "args": {"title": "Personal key verification"},
            "request_key": uuid.uuid4().hex,
        },
    )
    assert op["status"] == "completed" and op["result"]["user_id"] == uid
    client.execute("chat.delete", {"chat_id": op["result"]["chat_id"]})
    with SessionLocal() as db:
        ApiKeyService(db).set_enabled(uid, key_id, False)
    try:
        client.request("GET", "/v1/personal-agent/session-manager/devices")
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("Disabled API Key still authorized session access")
    print("PASS: actual personal API-Key owner, session creation, and immediate Key revocation")
finally:
    with SessionLocal() as db:
        ApiKeyService(db).set_enabled(uid, key_id, False)

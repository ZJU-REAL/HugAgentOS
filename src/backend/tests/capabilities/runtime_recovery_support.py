"""Regressions for desktop identity, explicit bindings and immutable runs."""

import base64
import json

import pytest
from core.capabilities import registry, manifest_order
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills


def _ordered_skill_manifest(st, manifest):
    return manifest_order.stamp(manifest, manifest_order.begin("skill", cloud_skills._profile(st)))


def state(uid):
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": uid, "c": "center-" + uid, "a": 1, "h": "session-" + uid, "d": "device"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {"cloud_base": "https://cloud.example", "token": f"dcap2.{body}.sig"}


def _zip(files):
    import io, zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def _intent(monkeypatch, body="v1"):
    from core.services.desktop_capability_protocol import skill_content_hash

    st = state("a")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    profile = profile_id(st["cloud_base"], "a")
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], "skill", "example", scope="shared"),
        content_hash=skill_content_hash(body, {}),
    )
    return st, inst


@pytest.fixture
def durable_index(index_db, monkeypatch):
    from core.db.engine import Base
    from core.services.model_config import ModelConfigService

    with index_db() as db:
        Base.metadata.create_all(db.get_bind())
    monkeypatch.setattr(ModelConfigService, "_instance", None)
    monkeypatch.setattr("core.services.model_config.SessionLocal", index_db)
    return index_db


def _shell_id(center: str) -> str:
    """本机影子用户按壳的命名空间规则建档：cloud:<host>:<port>:<ucid>。"""
    from core.services.desktop_cloud_bridge import shell_user_center_id

    return shell_user_center_id("https://cloud.example", center)


def _state_v2(uid, epoch=1, nonce="one"):
    claims = {
        "u": "cloud-" + uid,
        "c": "center-" + uid,
        "a": epoch,
        "h": "session-" + str(epoch),
        "d": "device",
        "n": nonce,
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"cloud_base": "https://cloud.example", "token": "dcap2." + body + ".sig"}

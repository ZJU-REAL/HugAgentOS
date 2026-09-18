"""插件的技能只在插件下露面，不跟着冒到技能库里。

从 `/v1/catalog` 这一层验证：能力中心的技能列表就是这个接口返回的 ``skills``，
在这里剔干净，界面上才不会出现「启用插件后技能库里多出一条」。
"""

from __future__ import annotations

import base64
import json

import pytest
from api.routes.v1.catalog import router
from core.auth.backend import UserContext, get_current_user
from core.capabilities import device_catalog, registry
from core.capabilities.paths import KIND_PLUGIN, KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from core.db.engine import get_db
from core.services import desktop_cloud_bridge as bridge
from fastapi import FastAPI
from fastapi.testclient import TestClient

_CLOUD = "https://cloud.example"
_USER = "u1"


def _token(uid: str) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"u": uid, "c": "center", "a": 1, "h": "session", "d": "device"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"dcap2.{body}.sig"


@pytest.fixture
def client(tmp_path, index_db, caps_root, monkeypatch):
    from core.config.catalog_runtime import invalidate_runtime_catalog_cache
    from core.db.engine import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(bridge, "get_state", lambda: {"cloud_base": _CLOUD, "token": _token(_USER)})
    monkeypatch.setattr("core.auth.desktop_bridge.bridge_enabled", lambda: True)
    # 云端下发的连接器与本次断言无关，固定成空，避免依赖清单缓存。
    monkeypatch.setattr(device_catalog, "_managed_connectors", lambda: [])

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    invalidate_runtime_catalog_cache()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=_USER, user_center_id=_USER, username=_USER
    )
    yield TestClient(app)
    session.close()
    engine.dispose()
    invalidate_runtime_catalog_cache()


def _install(kind: str, key: str, **kwargs):
    return registry.upsert(
        profile_id=profile_id(_CLOUD, _USER),
        ref=cloud_ref(_CLOUD, kind, key, scope="shared"),
        display_name=kwargs.pop("display_name", key),
        **kwargs,
    )


def _skill_ids(client) -> list:
    response = client.get("/v1/catalog")
    assert response.status_code == 200
    return [item["id"] for item in response.json()["data"]["skills"]]


def test_plugin_skill_is_not_listed_in_the_skill_library(client):
    _install(KIND_PLUGIN, "feishu", payload={"components": {"skills": ["feishu-doc"]}})
    _install(KIND_SKILL, "feishu-doc", source_plugin="feishu")
    _install(KIND_SKILL, "market-x")

    ids = _skill_ids(client)

    assert "market-x" in ids
    assert "feishu-doc" not in ids


def test_plugin_skill_stays_hidden_before_its_plugin_syncs(client):
    """技能清单和插件清单是两次同步，中间这段窗口也不该漏。"""
    _install(KIND_SKILL, "feishu-doc", source_plugin="feishu")

    assert "feishu-doc" not in _skill_ids(client)


def test_plugin_skill_stays_hidden_without_an_ownership_tag(client):
    """历史条目没有归属标记时，插件登记的组件边仍然兜得住。"""
    _install(KIND_PLUGIN, "feishu", payload={"components": {"skills": ["feishu-doc"]}})
    _install(KIND_SKILL, "feishu-doc")

    assert "feishu-doc" not in _skill_ids(client)

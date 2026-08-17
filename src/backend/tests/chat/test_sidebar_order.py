"""侧边栏手动拖拽顺序接口（/v1/chats/sidebar-order）。

顺序表是纯 UI 偏好，存 users_shadow.metadata。这里守两件事：读写都要去重清洗，
以及写入落到 metadata 的键名不能漂——前端按这个键名读回顺序。
"""

import asyncio

from api.routes.v1 import chats as chats_route


class _StubUser:
    user_id = "user_1"


class _StubUserService:
    """替身：记录 update_user_metadata 的入参，get_user_settings 返回预置内容。"""

    settings: dict = {}
    captured: dict = {}

    def __init__(self, db):
        self.db = db

    def get_user_settings(self, user_id):
        return dict(type(self).settings)

    def update_user_metadata(self, user_id, patch):
        type(self).captured = {"user_id": user_id, "patch": patch}


def test_dedup_id_list_strips_blanks_and_duplicates():
    assert chats_route._dedup_id_list(["a", " b ", "a", "", "  ", "c"]) == ["a", "b", "c"]
    # 非列表（历史脏数据 / 手改过的 metadata）不能炸，退成空
    assert chats_route._dedup_id_list(None) == []
    assert chats_route._dedup_id_list("abc") == []


def test_get_sidebar_order_cleans_stored_value(monkeypatch):
    _StubUserService.settings = {chats_route.SIDEBAR_ORDER_KEY: ["c1", "c1", " c2 ", ""]}
    monkeypatch.setattr(chats_route, "UserService", _StubUserService)

    resp = asyncio.run(chats_route.get_sidebar_order(user=_StubUser(), db=object()))

    assert resp["data"]["order"] == ["c1", "c2"]


def test_get_sidebar_order_defaults_to_empty(monkeypatch):
    """没拖过的账号返回空数组——前端据此退回默认排序。"""
    _StubUserService.settings = {}
    monkeypatch.setattr(chats_route, "UserService", _StubUserService)

    resp = asyncio.run(chats_route.get_sidebar_order(user=_StubUser(), db=object()))

    assert resp["data"]["order"] == []


def test_update_sidebar_order_persists_cleaned_order(monkeypatch):
    _StubUserService.captured = {}
    monkeypatch.setattr(chats_route, "UserService", _StubUserService)
    body = chats_route.UpdateSidebarOrderRequest(order=["c2", "c1", "c2", " "])

    resp = asyncio.run(
        chats_route.update_sidebar_order(request=body, user=_StubUser(), db=object())
    )

    assert resp["data"]["order"] == ["c2", "c1"]
    assert _StubUserService.captured["user_id"] == "user_1"
    assert _StubUserService.captured["patch"] == {chats_route.SIDEBAR_ORDER_KEY: ["c2", "c1"]}


def test_update_sidebar_order_truncates_to_cap(monkeypatch):
    _StubUserService.captured = {}
    monkeypatch.setattr(chats_route, "UserService", _StubUserService)
    ids = [f"c{i}" for i in range(chats_route.SIDEBAR_ORDER_MAX + 30)]

    resp = asyncio.run(
        chats_route.update_sidebar_order(
            request=chats_route.UpdateSidebarOrderRequest(order=ids),
            user=_StubUser(),
            db=object(),
        )
    )

    assert len(resp["data"]["order"]) == chats_route.SIDEBAR_ORDER_MAX
    assert resp["data"]["order"][0] == "c0"


def test_update_sidebar_order_empty_resets(monkeypatch):
    """空数组＝恢复默认排序，必须真的把键写成空，而不是跳过写入。"""
    _StubUserService.captured = {}
    monkeypatch.setattr(chats_route, "UserService", _StubUserService)

    asyncio.run(
        chats_route.update_sidebar_order(
            request=chats_route.UpdateSidebarOrderRequest(order=[]),
            user=_StubUser(),
            db=object(),
        )
    )

    assert _StubUserService.captured["patch"] == {chats_route.SIDEBAR_ORDER_KEY: []}

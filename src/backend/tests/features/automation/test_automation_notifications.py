"""通知中心：换存储格式不许沿用旧键，读不出来也不许伪装成"没有通知"。

这个 feed 从前是 Redis 列表（``LPUSH``），搬到短时状态层后改成了一个 JSON 字符串，
**键名却没变**。于是 Redis 部署上凡是还留着旧列表键的用户，每次读都 WRONGTYPE，读取
路径把它变成空列表——界面看不出坏了，只像是"一直没有通知"；更糟的是 ``push`` 要先读
再写，所以连新通知也写不进去，一直僵到键的 7 天 TTL 到期。生产日志里 409 条
``automation_notifications_read_failed`` 就是这么来的。

所以有三条硬约束：
  1. 键名带格式版本，换了写法就换版本，旧版本的值自己过期，不可能再撞上；
  2. 值坏掉时要就地清掉，让下一条通知能重新开张，而不是僵到 TTI 到期；
  3. 底层读失败要按 error 记且带上用户，不能和"确实没有通知"长得一样。
"""

import json

import pytest
from core.infra.ephemeral import LocalEphemeralState
from features.automation import notifications as notif

USER = "user_notify"


@pytest.fixture
def state(monkeypatch):
    store = LocalEphemeralState()
    monkeypatch.setattr(notif, "get_ephemeral_state", lambda: store)
    return store


async def test_push_then_list(state):
    await notif.push(USER, {"id": "n1", "title": "第一条"})
    await notif.push(USER, {"id": "n2", "title": "第二条"})

    got = await notif.list_recent(USER)

    assert [e["id"] for e in got] == ["n2", "n1"]


async def test_key_carries_the_format_version(state):
    # 旧版本写下的值必须落在另一个键上，永远不会被当成本版本的数据读出来
    assert notif._key(USER) == f"jx:notifications:v{notif.FORMAT_VERSION}:{USER}"
    assert "v1:" not in notif._key(USER)

    await state.put(f"jx:notifications:{USER}", json.dumps([{"id": "旧"}]), ttl=60)

    assert await notif.list_recent(USER) == []


async def test_unreadable_value_is_cleared_so_writes_recover(state):
    await state.put(notif._key(USER), "{ 这不是数组", ttl=60)

    assert await notif.list_recent(USER) == []
    # 关键：坏值清掉之后，新通知必须写得进去（原来的 bug 是连写都卡死）
    await notif.push(USER, {"id": "n1", "title": "恢复了"})

    assert [e["id"] for e in await notif.list_recent(USER)] == ["n1"]


async def test_wrong_shape_is_cleared(state):
    await state.put(notif._key(USER), json.dumps({"not": "a list"}), ttl=60)

    assert await notif.list_recent(USER) == []
    assert await state.get(notif._key(USER)) is None


async def test_backend_failure_is_logged_at_error_not_silently_empty(state, monkeypatch):
    logged = {}

    async def _boom(key):
        raise RuntimeError("WRONGTYPE Operation against a key holding the wrong kind of value")

    monkeypatch.setattr(state, "get", _boom)
    monkeypatch.setattr(
        notif.logger, "error", lambda event, **kw: logged.update({"event": event, **kw})
    )

    assert await notif.list_recent(USER) == []
    assert logged["event"] == "automation_notifications_read_failed"
    assert logged["user_id"] == USER


async def test_cap_and_modify(state):
    for i in range(notif.MAX_ENTRIES + 10):
        await notif.push(USER, {"id": f"n{i}", "read": False})
    assert len(await notif.list_recent(USER)) == notif.MAX_ENTRIES

    newest = f"n{notif.MAX_ENTRIES + 9}"
    await notif.modify(USER, {newest}, lambda e: {**e, "read": True})
    assert (await notif.list_recent(USER))[0]["read"] is True

    await notif.modify(USER, {newest}, lambda _: None)
    assert all(e["id"] != newest for e in await notif.list_recent(USER))

"""作业 SDK 的台账回写回归测试 —— 对应一次「跑了等于没跑」的线上事故。

事故经过：一个 568 项的补全作业跑了近一小时，子智能体调用真的在发（job_calls 有记录、
也有成功的），但台账 568 项**全程 pending、attempts 全 0**，最后导出是空的、成果全丢。

根因在 ``job.map``：脚本按文档用 ``ledger.seed([{"key": ..., "payload": ...}])`` 播种，
却把**原始业务对象**（``{"seq": 7, "name": ...}``）交给 ``job.map``——两者形状不同是常态。
当时 ``_key_of`` 只认 ``it["key"]``，取不到就返回 None，于是回写分支被**静默跳过**：
不报错、不打日志、外面完全看不出异常。

所以这里锁三件事：
1. 常见业务主键（seq/id/item_key）必须能兜住，绝不静默丢账；
2. 实在取不到主键时必须**喊出来**（log），因为静默丢账是最坏的失败模式；
3. 异常项要记 failed 且 bump_attempts，续跑才有依据。

SDK 是以字符串形式注入沙箱的（``SDK_SOURCE``），测试直接 exec 它，验的就是真正下发的那份代码。
"""

import re

import pytest

from orchestration.job_runtime import SDK_SOURCE


@pytest.fixture()
def sdk():
    """exec 出一份 SDK 命名空间，并把回调层换成记录器。

    ``_Ledger``/``log`` 里的 ``_post`` 是模块级名字，exec 进同一个 dict 后替换即可生效
    （闭包在调用时才解析全局名）。
    """
    ns: dict = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    calls: list = []

    def fake_post(path, payload, timeout=180):
        calls.append((path, payload))
        if path == "ledger" and payload.get("op") == "seed":
            return {"created": len(payload.get("items") or []), "skipped": 0}
        return {}

    ns["_post"] = fake_post
    ns["_calls"] = calls
    return ns


def _updates(ns):
    return [p for path, p in ns["_calls"] if path == "ledger" and p.get("op") == "update"]


def _logs(ns):
    return [str(p.get("message") or "") for path, p in ns["_calls"] if path == "log"]


@pytest.mark.parametrize(
    "item, expected_key",
    [
        ({"key": "r2", "v": 1}, "r2"),          # 文档里的标准形状
        ({"seq": 7, "v": 1}, "7"),              # 事故现场的形状：seq 当主键
        ({"id": "ent-9", "v": 1}, "ent-9"),
        ({"item_key": 42, "v": 1}, "42"),
    ],
)
def test_map_books_result_under_business_key(sdk, item, expected_key):
    """seed 与 map 的对象形状不一致是常态，主键必须能兜住——否则成果静默蒸发。"""
    sdk["job"].map([item], lambda it: {"ok": True})

    ups = _updates(sdk)
    assert len(ups) == 1, "每项都必须落一次账"
    assert ups[0]["key"] == expected_key
    assert ups[0]["status"] == "done"
    assert ups[0]["result"] == {"ok": True}


def test_map_honors_status_override(sdk):
    """`_status` 是脚本声明"查无"的唯一正道，不能被当成结果字段写进数据位。"""
    sdk["job"].map([{"seq": 1}], lambda it: {"_status": "not_found", "core": ""})

    ups = _updates(sdk)
    assert ups[0]["status"] == "not_found"
    assert "_status" not in ups[0]["result"]


def test_map_accepts_explicit_key_field_and_callable(sdk):
    sdk["job"].map([{"编号": "x1"}], lambda it: {"ok": 1}, key="编号")
    assert _updates(sdk)[0]["key"] == "x1"

    sdk["_calls"].clear()
    sdk["job"].map([{"a": 5}], lambda it: {"ok": 1}, key=lambda it: f"k{it['a']}")
    assert _updates(sdk)[0]["key"] == "k5"


def test_map_warns_loudly_when_key_is_unresolvable(sdk):
    """取不到主键时可以不落账，但**绝不允许安静**——静默丢账正是那次事故的形态。"""
    sdk["job"].map([{"名称": "甲"}, {"名称": "乙"}], lambda it: {"ok": 1})

    assert _updates(sdk) == [], "没有主键就不该瞎写台账"
    warned = [m for m in _logs(sdk) if "取不到台账主键" in m]
    assert len(warned) == 1, "必须告警，且只喊一次（别把日志刷爆）"
    assert "job.map" in warned[0]


def test_map_records_failure_with_attempts(sdk):
    """失败项要留下 failed + attempts，断点续跑才知道该重试谁。"""

    def boom(it):
        raise RuntimeError("搜索工具 503")

    sdk["job"].map([{"seq": 3}], boom)

    ups = _updates(sdk)
    assert len(ups) == 1
    assert ups[0]["status"] == "failed"
    assert ups[0]["bump_attempts"] is True
    assert "503" in ups[0]["error"]


def test_map_isolates_failures_across_items(sdk):
    """一项炸掉不能拖垮其余项 —— 这是 map 的核心契约。"""

    def half(it):
        if it["seq"] % 2 == 0:
            raise RuntimeError("nope")
        return {"ok": 1}

    sdk["job"].map([{"seq": i} for i in range(1, 5)], half, concurrency=2)

    ups = _updates(sdk)
    assert len(ups) == 4
    assert sorted(u["status"] for u in ups) == ["done", "done", "failed", "failed"]


def test_map_returns_none_means_self_managed(sdk):
    """fn 返回 None = 我自己写过账了，SDK 不得再插一脚。"""
    sdk["job"].map([{"seq": 1}], lambda it: None)
    assert _updates(sdk) == []


def test_sdk_docstring_warns_about_key_shape():
    """SDK 是照着注入沙箱的，说明必须写在源码里 —— 模型只看得到这一份。"""
    m = re.search(r"def map\(self.*?\"\"\"(.*?)\"\"\"", SDK_SOURCE, re.S)
    assert m, "job.map 的 docstring 不该消失，它是模型唯一的使用说明"
    assert "key" in m.group(1)

"""作业韧性回归 —— 对应一次"跑满一小时、进度停在 0、还自己复制出第二个作业"的事故。

事故链条（每一环都不报错，这是它最贵的地方）：

1. 脚本直接 ``job.map`` 没建台账 → 回写全部打到不存在的行，被静默丢弃 → 进度恒为 0；
2. 脚本传 ``item_key=it["seq"]``（int），而请求体只声明了 ``str`` → FastAPI **422**，
   模型一次都没被调用；
3. 每项 3ms 失败 × 568 项 × 并发 8 → 回调打爆网关限流 → **429**；
4. SDK 把 4xx 一律当"契约错误不重试"，而 429 恰好是 4xx → 连 runner 上报终态那一发
   也被拒 → 作业永远停在 ``running``；
5. 驱动只有 2 小时墙钟兜底，期间按间隔叫醒智能体报停滞，智能体于是又交了一份新作业。

这里逐条锁住修复后的行为。
"""

import json
import re
from types import SimpleNamespace

import pytest

from api.routes.v1.internal_jobs import AgentBody, LedgerBody
from core.chat.tool_log import _payload_carries_error
from orchestration.job_runtime import SDK_SOURCE, _final_from_marker


# ── ② 主键类型：int 必须能进门 ──────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [(7, "7"), ("7", "7"), (0, "0"), (None, None), ("", None)])
def test_item_key_accepts_int(raw, expected):
    """业务主键十有八九是行号——只收 str 等于把整轮作业挡在门外。"""
    assert AgentBody(prompt="x", item_key=raw).item_key == expected


def test_ledger_key_accepts_int():
    assert LedgerBody(op="update", key=42).key == 42


# ── ④ 429 是限流不是契约错误 ────────────────────────────────────────


class _FakeHTTPError(Exception):
    def __init__(self, code):
        self.code = code

    def read(self):
        return b"rate limited"


def _sdk_ns(responses):
    """exec 出 SDK 命名空间，把 urllib 整个换成按剧本回放的假模块。

    注意必须换成**独立的假对象**，不能去改 ns["urllib"]——那是真模块，改了会污染
    整个进程里其他用到 urllib 的测试。
    """
    ns: dict = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    ns["BASE"] = "http://callback.test/api"
    ns["JOB_ID"] = "job_test"
    ns["time"] = SimpleNamespace(sleep=lambda *_a, **_k: None)  # 别在测试里真等退避
    calls = {"n": 0}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": {"ok": True}}).encode()

    def fake_urlopen(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        item = responses[min(i, len(responses) - 1)]
        if isinstance(item, int):
            raise _FakeHTTPError(item)
        return Resp()

    ns["urllib"] = SimpleNamespace(
        request=SimpleNamespace(urlopen=fake_urlopen, Request=lambda *a, **k: SimpleNamespace(
            add_header=lambda *_a, **_k: None
        )),
        error=SimpleNamespace(HTTPError=_FakeHTTPError),
    )
    ns["_calls"] = calls
    return ns


def test_429_is_retried_not_raised():
    """限流是"待会儿再来"。当成契约错误直接抛，曾让终态上报也一起丢掉。"""
    ns = _sdk_ns([429, 429, "ok"])
    out = ns["_post"]("log", {"message": "hi"})
    assert out == {"ok": True}
    assert ns["_calls"]["n"] == 3, "429 必须重试到成功"


def test_real_contract_errors_still_fail_fast():
    """400/422 是真写错了，重试毫无意义，必须立刻抛。"""
    ns = _sdk_ns([422, "ok"])
    with pytest.raises(Exception) as ei:
        ns["_post"]("agent", {})
    assert "422" in str(ei.value)
    assert ns["_calls"]["n"] == 1, "契约错误不该重试"


# ── ① 打空台账必须被喊出来 ──────────────────────────────────────────


def test_update_warns_when_key_unknown():
    """后端说这个 key 不在台账里（多半漏了 seed）→ 必须留痕，不能静默丢。"""
    ns: dict = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    logged = []

    def fake_post(path, payload, timeout=180):
        if path == "log":
            logged.append(str(payload.get("message") or ""))
            return {}
        return {"ok": False, "known_key": False}

    ns["_post"] = fake_post
    ns["ledger"].update("42", status="done", result={})

    assert any("漏了 ledger.seed" in m for m in logged), "打空台账必须告警"


def test_map_surfaces_first_failure():
    """异常隔离不等于吞掉：整批全挂时，日志里必须看得见第一个原因。"""
    ns: dict = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    logged = []

    def fake_post(path, payload, timeout=180):
        if path == "log":
            logged.append(str(payload.get("message") or ""))
        return {"ok": True, "known_key": True}

    ns["_post"] = fake_post

    def boom(it):
        raise RuntimeError("HTTP 422 item_key")

    ns["job"].map([{"seq": i} for i in range(3)], boom)
    assert any("首个失败项" in m and "422" in m for m in logged)


# ── ⑤ runner 死了要能就地判终态 ─────────────────────────────────────


def test_final_from_marker_reads_落盘终态():
    tail = 'noise\n{"status": "failed", "error": "boom"}\nmore noise'
    assert _final_from_marker(tail) == ("failed", "boom")


def test_final_from_marker_defaults_to_failed():
    """捡不到标记就按 failed —— 进程没了而作业还 running，本来就不是正常收尾。"""
    assert _final_from_marker("just a traceback")[0] == "failed"
    assert _final_from_marker("")[0] == "failed"


# ── 观测面：载荷里写着 error 就不能记成 success ──────────────────────


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"error": "internet_search 调用失败: 429"}, True),
        ('{"error": "boom", "result": []}', True),
        ({"error": ""}, False),
        ({"result": ["ok"]}, False),
        ("一篇讲 error 处理的网页正文", False),
        (["block"], False),
        # MCP 工具真正的返回形状：内容块里裹着 JSON 文本，必须穿透
        ([{"type": "text", "text": '{"error": "internet_search 调用失败: 429", "result": []}'}], True),
        ([{"type": "text", "text": '{"result": [{"title": "错误码 429 是什么"}]}'}], False),
        ([{"type": "text", "text": "普通正文，没有 JSON"}], False),
    ],
)
def test_error_payload_detection(payload, expected):
    assert _payload_carries_error(payload) is expected


# ── SDK 说明必须跟着代码走（模型只看得到这一份）────────────────────


def test_sdk_documents_rate_limit_behaviour():
    m = re.search(r"# 4xx 是契约问题.*?\n(.*?\n){0,3}", SDK_SOURCE)
    assert m and "429" in m.group(0), "429 的例外必须写在 SDK 源码注释里"

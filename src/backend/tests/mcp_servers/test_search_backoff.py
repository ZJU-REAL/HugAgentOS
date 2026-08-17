"""联网搜索对上游限流/瞬时故障的退避 —— 一次 429 的代价是整个工作项白跑。

批量作业里，子智能体拿不到搜索结果就判定"未取得证据"、该项记 failed 等续跑。所以对
429/5xx 做有界重试是值得的：单次请求的成本远低于重跑整项。

⚠️ 这里**只做重试，不做全局限速**。曾经加过一版进程级最小间隔，实测把整个搜索 MCP
拖成每分钟 1 次、单次 30~393 秒——因为 MCP 工具处理器是 async 而 impl 是同步的，
限速的 sleep 直接睡在事件循环上、还持着锁，把所有用户和作业的搜索串成一条队。
那一版已回退；要再碰限速，先解决"同步实现跑在事件循环上"这个前提。
"""

import time
import types

import pytest

from mcp_servers.internet_search_mcp import impl


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.headers = {}


@pytest.fixture()
def _no_sleep(monkeypatch):
    monkeypatch.setattr(
        impl, "time", types.SimpleNamespace(sleep=lambda *_: None, monotonic=time.monotonic)
    )


def test_backoff_retries_429_then_succeeds(monkeypatch, _no_sleep):
    calls = {"n": 0}

    class C:
        def post(self, *a, **k):
            calls["n"] += 1
            return _Resp(429 if calls["n"] < 3 else 200)

    monkeypatch.setattr(impl, "_get_httpx_client", lambda: C())
    assert impl._post_with_backoff("http://x", headers={}, json={}).status_code == 200
    assert calls["n"] == 3


def test_backoff_gives_up_and_returns_last_response(monkeypatch, _no_sleep):
    """退避预算耗尽要把最后一发原样交回去 —— 上层据此报"工具不可用"，不能伪装成成功。"""
    calls = {"n": 0}

    class C:
        def post(self, *a, **k):
            calls["n"] += 1
            return _Resp(429)

    monkeypatch.setattr(impl, "_get_httpx_client", lambda: C())
    resp = impl._post_with_backoff("http://x", headers={}, json={})
    assert resp.status_code == 429
    assert calls["n"] == impl._MAX_RETRIES + 1


def test_backoff_does_not_retry_client_errors(monkeypatch, _no_sleep):
    """400 是请求写错了，重试毫无意义。"""
    calls = {"n": 0}

    class C:
        def post(self, *a, **k):
            calls["n"] += 1
            return _Resp(400)

    monkeypatch.setattr(impl, "_get_httpx_client", lambda: C())
    assert impl._post_with_backoff("http://x", headers={}, json={}).status_code == 400
    assert calls["n"] == 1


def test_retry_after_header_is_honored(monkeypatch):
    """上游明说了等多久就等多久，别自作聪明地更早重试。"""
    waited: list = []
    monkeypatch.setattr(
        impl, "time", types.SimpleNamespace(sleep=lambda s: waited.append(s), monotonic=time.monotonic)
    )
    calls = {"n": 0}

    class C:
        def post(self, *a, **k):
            calls["n"] += 1
            r = _Resp(429 if calls["n"] == 1 else 200)
            if calls["n"] == 1:
                r.headers = {"Retry-After": "7"}
            return r

    monkeypatch.setattr(impl, "_get_httpx_client", lambda: C())
    impl._post_with_backoff("http://x", headers={}, json={})
    assert waited and waited[0] >= 7.0, waited


def test_no_global_pacing_left_behind():
    """守住回退：进程级限速不能再溜回来（它会把整个搜索 MCP 串成一条队）。"""
    assert not hasattr(impl, "_pace"), "全局限速已回退，不应重新出现"
    assert not hasattr(impl, "_SEARCH_MIN_INTERVAL_S")

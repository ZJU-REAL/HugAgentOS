"""能力变更号：只认增删，并且跨进程一致。

线上后端跑多个 uvicorn worker。信号一旦是进程内计数器，同一时刻两个进程会给出不同的
值，客户端轮流打到不同进程就会不停看到「变了」——实测生产上两个 worker 的计数分别停在
9 和 13，客户端因此一天触发上千次同步。所以这里既要验「什么时候变」，也要验「不同进程
算出来的是同一个值」。
"""

from __future__ import annotations

import pytest

from core.capabilities import change_signal


@pytest.fixture(autouse=True)
def _fresh_cache():
    change_signal.invalidate()
    yield
    change_signal.invalidate()


def _stub_tables(monkeypatch, rows):
    """把指纹的数据来源换成给定的行数/最新创建时间。"""
    monkeypatch.setattr(change_signal, "_fingerprint", lambda: "|".join(rows))


def test_the_value_is_derived_from_data_so_every_worker_agrees(monkeypatch):
    _stub_tables(monkeypatch, ["skills:3:t1", "agents:2:t2"])
    first = change_signal.current()

    # 另一个 worker：独立的模块状态，但数据一样。
    change_signal.invalidate()
    second = change_signal.current()

    assert first == second != ""


def test_adding_or_removing_one_changes_the_value(monkeypatch):
    _stub_tables(monkeypatch, ["skills:3:t1"])
    before = change_signal.current()

    _stub_tables(monkeypatch, ["skills:4:t1"])
    change_signal.invalidate()
    assert change_signal.current() != before


def test_replacing_one_in_place_still_changes_the_value(monkeypatch):
    """删一条又加一条：行数没变，但最新创建时间变了。"""
    _stub_tables(monkeypatch, ["skills:3:t1"])
    before = change_signal.current()

    _stub_tables(monkeypatch, ["skills:3:t9"])
    change_signal.invalidate()
    assert change_signal.current() != before


def test_toggling_or_editing_does_not_change_the_value(monkeypatch):
    """启停与改内容不动行数，也不动创建时间——不该产生同步信号。"""
    _stub_tables(monkeypatch, ["skills:3:t1", "agents:2:t2"])
    before = change_signal.current()

    change_signal.invalidate()
    assert change_signal.current() == before


def test_an_unreadable_database_keeps_the_last_value(monkeypatch):
    """信号取不到时保持上一次的值，绝不因此凭空产生一次「变了」。"""
    _stub_tables(monkeypatch, ["skills:3:t1"])
    known = change_signal.current()

    monkeypatch.setattr(change_signal, "_fingerprint", lambda: "")
    change_signal.invalidate()
    assert change_signal.current() == known

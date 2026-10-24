"""每个流式事件都要证明自己仍是这轮的合法作者——但不必为此每个字查一次库。

一次 ``require_lease`` 在部署里实测约 0.5ms（其中约四分之三是取 Session 与连接池
pre-ping，不是查询本身）。挂在每个 token 上，四十条并发流就要花掉整整一个核，去问一个
几乎不会变的问题。

取而代之的三条性质合起来才等价于原来的"每事件都证明"，缺一不可：

* 本进程自己发现的租约丢失（心跳失败、或取消就发生在本进程）立刻熔断，且不查库；
* 别的进程做出的判定只能经共享 journal 传来，所以回库检查不能取消，只能限频——
  静默超过 ``_CANCEL_VISIBILITY_SEC`` 后的下一个事件必定回库复核；
* 终止事件走的是带租约的**写**，所以一轮的收尾永远硬熔断，不受上面的预算影响。

另外明确一件容易混淆的事：这条链路决定的是"谁能写"，不是"写下的东西是否留得住"。
中断后内容能否恢复，靠的是事件流、每两秒的 ``chat_messages`` 检查点，以及取消时直接
落库的 partial，与本测试无关。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from core.runs.events.writer import EventOffsets
from core.runs.settings import _CANCEL_VISIBILITY_SEC, _OFFSET_RESERVATION
from core.runs.journal import RunLeaseLost


class CountingJournal:
    """记下真实 journal 会为此付出的数据库往返次数。"""

    def __init__(self) -> None:
        self.reserves = 0
        self.lease_reads = 0
        self.terminal_allocations = 0
        self._next = 0

    def allocate_event_offset(self, run_id, owner=None, count=1, terminal=False):
        self.reserves += 1
        if terminal:
            self.terminal_allocations += 1
        base = self._next
        self._next += count
        return base

    def require_lease(self, run_id, owner):
        self.lease_reads += 1


def _offsets(lease_lost=None):
    journal = CountingJournal()
    return journal, EventOffsets(
        journal, "run", "owner", lease_lost=lease_lost or asyncio.Event()
    )


@pytest.mark.asyncio
async def test_a_fast_stream_reads_the_lease_on_a_budget_not_per_event():
    journal, offsets = _offsets()

    events = 400
    started = time.monotonic()
    for _ in range(events):
        await offsets.take()
    elapsed = time.monotonic() - started

    # 原先：除去每 _OFFSET_RESERVATION 个事件一次的预留写，其余每事件各查一次库。
    assert journal.lease_reads < events - (events // _OFFSET_RESERVATION)
    # 现在：上限由时间决定，与事件数无关——这正是"与吐字速度解耦"的含义。
    assert journal.lease_reads <= int(elapsed / _CANCEL_VISIBILITY_SEC) + 2


@pytest.mark.asyncio
async def test_in_process_lease_loss_fences_the_next_event_without_a_query():
    lease_lost = asyncio.Event()
    journal, offsets = _offsets(lease_lost)
    await offsets.take()
    reads_before = journal.lease_reads

    lease_lost.set()

    with pytest.raises(RunLeaseLost):
        await offsets.take()
    assert journal.lease_reads == reads_before


@pytest.mark.asyncio
async def test_a_quiet_gap_forces_the_next_event_back_to_the_journal():
    """别的进程发来的取消只有这条通道能传进来，所以它必须真的会发生。"""
    journal, offsets = _offsets()
    await offsets.take()
    await asyncio.sleep(_CANCEL_VISIBILITY_SEC + 0.02)
    reads_before = journal.lease_reads

    await offsets.take()

    assert journal.lease_reads == reads_before + 1


@pytest.mark.asyncio
async def test_the_terminal_event_always_fences_on_an_owned_write():
    """收尾不吃预算：终止 offset 由带租约的写分配，被接管的 worker 到这里必定停下。"""
    journal, offsets = _offsets()
    await offsets.take()
    reads_before = journal.lease_reads

    await offsets.take(terminal=True)

    assert journal.terminal_allocations == 1
    assert journal.lease_reads == reads_before  # 走的是写，不是那条被限频的读


@pytest.mark.asyncio
async def test_offsets_stay_contiguous_across_reservation_boundaries():
    _journal, offsets = _offsets()

    taken = [await offsets.take() for _ in range(_OFFSET_RESERVATION * 2 + 5)]

    assert taken == list(range(len(taken)))


@pytest.mark.asyncio
async def test_last_tracks_the_offset_including_the_terminal_one():
    """检查点把 ``last`` 当作"这一轮产出到哪儿了"写进库，续播的客户端照它定位。

    终止事件单独取号（要排在所有活动 offset 之后），这条路径一旦忘了更新 ``last``，
    收尾写进去的就还是上一个事件的号——错得很安静。
    """
    _journal, offsets = _offsets()
    first = await offsets.take()
    assert offsets.last == first

    terminal = await offsets.take(terminal=True)

    assert offsets.last == terminal

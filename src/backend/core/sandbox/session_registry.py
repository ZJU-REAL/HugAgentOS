"""一个会话对应哪个沙箱容器——这条绑定的真源，跨进程共享。

后端从一个 uvicorn 进程变成 N 个之后（``WEB_CONCURRENCY``），沙箱这条链路上有一件
事被漏掉了：``OpenSandboxProvider._sessions`` 是**进程内**的字典，预热池同样只活在
进程生命周期内。于是同一个会话的相邻两轮落到不同 worker，各自会去建自己的容器，
两个都活着、各自保活、各有各的 ``/workspace``。现象是「跨轮随机跳容器」：上一轮
nohup 起的进程和 ``run.log`` 一起"消失"，其实它们在另一台容器里好好跑着。产物散在
两台，断点续跑的判据会读到残缺事实。

``core/infra/worker_count.py`` 列举了「哪些东西活在进程内、因此必须锁死单进程」，
沙箱会话表就是漏掉的那一条。补的办法不是把 worker 数压回 1，而是把这条绑定搬出
进程：谁先拿到 :func:`claim` 谁建容器并登记，后来的 worker 查到登记就连到同一个
容器上。

存放处是 :mod:`core.infra.ephemeral` —— 仓库里既有的 TTL keyspace，有 Redis 就用
Redis、没有就用进程内那份（单进程部署本来就只允许一个 worker）。用它而不是自己再
写一套双后端，顺带拿到三件事：条目自带过期、进程内那份跨事件循环安全（子智能体跑
在自己的循环上），以及和 ``core.infra.leader`` 等模块同一套语义。

绑定同时写正反两个方向，反向那条（容器 → 会话）见 ``_REVERSE_KEY``。

绑定和「最后一次使用」写在同一条记录里，所以记一次使用就是一次写、没有读改写。
最后使用时间必须共享：空闲回收按「这个会话多久没动」决定要不要快照+销毁，而一个
worker 只看得见自己服务过的轮次；不共享的话，连着五轮都由 B 处理时 A 会把 B 正在
用的容器销毁掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import AsyncIterator, Optional, Tuple

from core.infra.ephemeral import get_ephemeral_state

logger = logging.getLogger(__name__)

_KEY = "jx:sandbox:session:{session_id}"
_LOCK_KEY = "jx:sandbox:claim:{session_id}"
# 反向索引：从容器反查「它属于哪个会话」。正向那条够不着——问的人手里只有容器 id，
# 而 keyspace 只能按 key 取、不能反查值。沙箱空闲池靠它守住「队列里只有无主容器」：
# 队列条目的老化时间只在入队时打、不被真实使用刷新，少了这道判定就会把会话正在用的
# 容器当成闲置销毁掉（生产事故：``/workspace`` 连同产物一起没了）。
_REVERSE_KEY = "jx:sandbox:bound:{sandbox_id}"

# 认领只护住「查登记 → 连上去 / 新建 → 写登记」这一段。建容器最慢的路径是从快照恢复，
# 实测十几秒，所以锁的存活时间要盖得住它；持有者结束就主动删，正常不会等到过期。
_LOCK_TTL_S = 180
# 等锁用退避而不是定频轮询：一次 park 要十几秒，10Hz 会白打上百个来回。
_POLL_MIN_S = 0.05
_POLL_MAX_S = 0.5


def _binding_ttl_s() -> int:
    """登记比空闲回收窗口活得久，回收判断才读得到最后使用时间。"""
    from core.config.settings import settings

    return max(120, int(settings.sandbox.idle_ttl_s) * 2)


async def _read(session_id: str) -> Optional[Tuple[str, float]]:
    """``(sandbox_id, 最后使用时间)``；没有登记或记录读不懂返回 ``None``。"""
    raw = await get_ephemeral_state().get(_KEY.format(session_id=session_id))
    if not raw:
        return None
    try:
        entry = json.loads(raw)
        return str(entry["sandbox_id"]), float(entry["active_at"])
    except Exception:  # noqa: BLE001 — 坏记录当没有，下一次使用会重新写
        logger.warning("[sandbox-registry] unreadable binding for %s", session_id)
        return None


@contextlib.asynccontextmanager
async def claim(session_id: str) -> AsyncIterator[None]:
    """跨进程认领这个会话，期间只有一个 worker 能建 / 换它的沙箱。"""
    state = get_ephemeral_state()
    key = _LOCK_KEY.format(session_id=session_id)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + _LOCK_TTL_S
    delay = _POLL_MIN_S
    while not await state.hold(key, token, ttl=_LOCK_TTL_S):
        if time.monotonic() > deadline:
            # 锁本身带过期时间，等过头只可能是持有者卡死。继续往下走比把请求挂死好：
            # 最坏结果是两个 worker 同时建，后写的登记覆盖先写的，多出来的那个容器
            # 会被空闲回收收走。
            logger.warning(
                "[sandbox-registry] claim %s timed out; proceeding unclaimed", session_id
            )
            yield
            return
        await asyncio.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_S)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await state.drop(key)


async def lookup(session_id: str) -> Optional[str]:
    """这个会话当前绑定的沙箱 id；没有登记返回 ``None``。"""
    entry = await _read(session_id)
    return entry[0] if entry else None


async def remember(session_id: str, sandbox_id: str) -> None:
    """记下「这个会话用这台容器，刚刚用过」。建好之后和每次使用都调它。"""
    state = get_ephemeral_state()
    ttl = _binding_ttl_s()
    await state.put(
        _KEY.format(session_id=session_id),
        json.dumps({"sandbox_id": str(sandbox_id), "active_at": time.time()}),
        ttl=ttl,
    )
    # 反向索引和正向记录同写同活，否则长会话跑过一个 TTL 之后就问不出主人了。
    await state.put(_REVERSE_KEY.format(sandbox_id=sandbox_id), session_id, ttl=ttl)


async def forget(session_id: str, sandbox_id: str) -> None:
    """撤销登记——只在它仍指向这个沙箱时，免得抹掉别人刚建的那条。"""
    entry = await _read(session_id)
    if entry and entry[0] == str(sandbox_id):
        await get_ephemeral_state().drop(
            _KEY.format(session_id=session_id),
            _REVERSE_KEY.format(sandbox_id=sandbox_id),
        )


async def bound_session(sandbox_id: str) -> Optional[str]:
    """这台容器当前属于哪个会话；无主返回 ``None``。

    空闲池用它守住「队列里只有无主容器」，重启后的沙箱认领用它区分「无主，可以收进
    池」和「仍在服务会话，别碰」。反向索引会过期——会话换了容器之后，旧容器那条要等
    TTL 才消失——所以回正向记录复核一次，对不上就顺手清掉，两个方向只留一个事实。
    """
    state = get_ephemeral_state()
    key = _REVERSE_KEY.format(sandbox_id=sandbox_id)
    sid = await state.get(key)
    if not sid:
        return None
    entry = await _read(sid)
    if entry and entry[0] == str(sandbox_id):
        return sid
    await state.drop(key)
    return None


async def in_use_elsewhere(session_id: str, idle_threshold_s: float) -> bool:
    """这个会话最近是不是还有别人在用。

    空闲回收做的是「快照 + 销毁容器」，而一个 worker 只看得见自己服务过的轮次。
    所以判据必须是共享的最后使用时间。**读不到登记时按「在用」处理**——拿不准的
    时候不动手，最坏结果只是这一轮没回收，下一轮再来；反过来猜错就是把别人正在用
    的容器销毁掉。
    """
    try:
        entry = await _read(session_id)
    except Exception as exc:  # noqa: BLE001 — 登记读不到就不回收
        logger.warning(
            "[sandbox-registry] %s 最后使用时间读取失败，本轮跳过回收：%s", session_id, exc
        )
        return True
    return entry is not None and (time.time() - entry[1]) < idle_threshold_s

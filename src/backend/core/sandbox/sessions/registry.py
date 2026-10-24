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
from contextvars import ContextVar
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
_LOCK_RENEW_INTERVAL_S = 60


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


_lease_state: ContextVar[Optional[dict]] = ContextVar("sandbox_claim_lease", default=None)


def claim_active() -> bool:
    return _lease_state.get() is not None and claim_valid()


def claim_valid() -> bool:
    state = _lease_state.get()
    while state is not None:
        if not state["valid"]:
            return False
        state = state.get("parent")
    return True


@contextlib.asynccontextmanager
async def claim(session_id: str) -> AsyncIterator[None]:
    """跨进程认领这个会话，期间只有一个 worker 能建 / 换它的沙箱。"""
    from core.config.settings import settings
    sandbox = settings.sandbox
    ttl = max(_LOCK_TTL_S,
              2 * getattr(sandbox, "opensandbox_request_timeout_s", 120)
              + getattr(sandbox, "opensandbox_snapshot_wait_timeout_s", 120) + 60)
    state = get_ephemeral_state()
    key = _LOCK_KEY.format(session_id=session_id)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + ttl
    delay = _POLL_MIN_S
    while not await state.hold(key, token, ttl=ttl):
        if time.monotonic() > deadline:
            raise TimeoutError(f"sandbox session claim timed out: {session_id}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_S)
    owner = asyncio.current_task()
    lease_lost = False
    parent = _lease_state.get()
    # create_task copies ContextVars, but not ownership of a request's locks.
    # Accounting children may still observe that shared lease's invalidation;
    # a new independent claim must not inherit it as an ownership prerequisite.
    if parent is not None and parent.get("owner") is not owner:
        parent = None
    lease = {"valid": True, "parent": parent, "key": key, "token": token, "owner": owner}
    context_token = _lease_state.set(lease)

    async def renew_lease() -> None:
        nonlocal lease_lost
        while True:
            await asyncio.sleep(min(_LOCK_RENEW_INTERVAL_S, ttl / 3))
            try:
                retained = await asyncio.wait_for(
                    state.compare_exchange(key, token, token, ttl=ttl), timeout=ttl / 3
                )
            except Exception:
                retained = False
            if not retained:
                lease_lost = True
                lease["valid"] = False
                if owner is not None:
                    owner.cancel()
                return

    heartbeat = asyncio.create_task(renew_lease())
    try:
        yield
    except asyncio.CancelledError:
        if lease_lost:
            if owner is not None:
                owner.uncancel()
            raise RuntimeError(f"sandbox session claim lost: {session_id}") from None
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
        with contextlib.suppress(Exception):
            await state.release(key, token)
        _lease_state.reset(context_token)


async def lookup(session_id: str) -> Optional[str]:
    """这个会话当前绑定的沙箱 id；没有登记返回 ``None``。"""
    entry = await _read(session_id)
    return entry[0] if entry else None


async def remember(session_id: str, sandbox_id: str) -> None:
    """记下「这个会话用这台容器，刚刚用过」。建好之后和每次使用都调它。"""
    await _publish_binding(session_id, sandbox_id, time.time())


def _binding_guards() -> dict[str, str]:
    if not claim_valid():
        raise RuntimeError("sandbox binding lease lost")
    guards = {}
    lease = _lease_state.get()
    while lease is not None:
        guards[lease["key"]] = lease["token"]
        lease = lease.get("parent")
    return guards


def _binding_lease_lost() -> None:
    # Finally blocks must see loss even before the heartbeat gets another turn.
    lease = _lease_state.get()
    while lease is not None:
        lease["valid"] = False
        lease = lease.get("parent")
    raise RuntimeError("sandbox binding lease lost")


async def _publish_binding(session_id: str, sandbox_id: str, active_at: float) -> None:
    published = await get_ephemeral_state().mutate_if(_binding_guards(), {
        _KEY.format(session_id=session_id): json.dumps({
            "sandbox_id": str(sandbox_id), "active_at": active_at,
        }),
        _REVERSE_KEY.format(sandbox_id=sandbox_id): session_id,
    }, ttl=_binding_ttl_s())
    if not published:
        _binding_lease_lost()


async def renew_binding(session_id: str, sandbox_id: str) -> bool:
    """Extend a protected idle binding without pretending the user used it.

    Callers hold the session claim, so retirement cannot interleave the refresh.
    """
    entry = await _read(session_id)
    if entry is None or entry[0] != str(sandbox_id):
        return False
    await _publish_binding(session_id, sandbox_id, entry[1])
    return True


async def forget(session_id: str, sandbox_id: str) -> None:
    """Retire only the observed binding while the distributed claim still owns it."""
    state = get_ephemeral_state()
    key = _KEY.format(session_id=session_id)
    raw = await state.get(key)
    if not raw:
        return
    try:
        if str(json.loads(raw)["sandbox_id"]) != str(sandbox_id):
            return
    except (ValueError, KeyError, TypeError):
        return
    reverse = _REVERSE_KEY.format(sandbox_id=sandbox_id)
    reverse_value = await state.get(reverse)
    guards = {**_binding_guards(), key: raw, reverse: reverse_value}
    remove = (key, reverse) if reverse_value == session_id else (key,)
    if not await state.mutate_if(guards, {}, remove=remove, ttl=_binding_ttl_s()):
        _binding_lease_lost()


async def bound_session(sandbox_id: str) -> Optional[str]:
    """这台容器当前属于哪个会话；无主返回 ``None``。

    空闲池用它守住「队列里只有无主容器」，重启后的沙箱认领用它区分「无主，可以收进
    池」和「仍在服务会话，别碰」。反向索引会过期——会话换了容器之后，旧容器那条要等
    TTL 才消失——所以回正向记录复核一次；读取不修改共享记录，避免清掉并发更新。
    """
    state = get_ephemeral_state()
    key = _REVERSE_KEY.format(sandbox_id=sandbox_id)
    sid = await state.get(key)
    if not sid:
        return None
    entry = await _read(sid)
    if entry and entry[0] == str(sandbox_id):
        return sid
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


_TRACK_KEY = "jx:sandbox:tracked:{sandbox_id}"
_TRACK_TTL_S = 180


async def track(sandbox_id: str, worker_token: str) -> bool:
    return await get_ephemeral_state().hold(
        _TRACK_KEY.format(sandbox_id=sandbox_id), worker_token, ttl=_TRACK_TTL_S
    )


async def take_for_destruction(
    sandbox_id: str, expected_owner: Optional[str], destruction_token: str,
    *, ttl: int = _TRACK_TTL_S,
) -> bool:
    """Atomically replace the expected tracker or claim a now-untracked key."""
    state = get_ephemeral_state()
    key = _TRACK_KEY.format(sandbox_id=sandbox_id)
    if expected_owner is not None and await state.compare_exchange(
        key, expected_owner, destruction_token, ttl=ttl,
    ):
        return True
    return await state.hold(key, destruction_token, ttl=ttl)


@contextlib.asynccontextmanager
async def destruction_guard(
    sandbox_id: str, expected_owner: Optional[str] = None,
) -> AsyncIterator[bool]:
    """Hold an independent renewable destruction lease until the call finishes."""
    from core.config.settings import settings

    ttl = max(_TRACK_TTL_S,
              getattr(settings.sandbox, "opensandbox_request_timeout_s", 120) + 60)
    token = uuid.uuid4().hex
    key = _TRACK_KEY.format(sandbox_id=sandbox_id)
    state = get_ephemeral_state()
    acquired = False
    completed = False
    owner = asyncio.current_task()
    lease_lost = False
    heartbeat = None

    async def renew_destruction():
        nonlocal lease_lost
        while True:
            await asyncio.sleep(min(_LOCK_RENEW_INTERVAL_S, ttl / 3))
            try:
                retained = await asyncio.wait_for(
                    state.compare_exchange(key, token, token, ttl=ttl), timeout=ttl / 3
                )
            except Exception:
                retained = False
            if not retained:
                lease_lost = True
                if owner is not None:
                    owner.cancel()
                return

    try:
        if claim_valid() and not await bound_session(sandbox_id):
            acquired = await take_for_destruction(
                sandbox_id, expected_owner, token, ttl=ttl,
            )
        if acquired:
            heartbeat = asyncio.create_task(renew_destruction())
        yield bool(acquired and claim_valid() and not await bound_session(sandbox_id))
        if lease_lost:
            raise RuntimeError(f"sandbox destruction lease lost: {sandbox_id}")
        completed = True
    except asyncio.CancelledError:
        if lease_lost:
            if owner is not None:
                owner.uncancel()
            raise RuntimeError(f"sandbox destruction lease lost: {sandbox_id}") from None
        raise
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
        # A cancelled or failed DELETE may still be executing remotely. Stop
        # renewal but keep its finite fence until TTL expiry; only acknowledged
        # completion authorizes immediate adoption.
        if acquired and completed:
            try:
                await untrack(sandbox_id, token)
            except Exception as exc:
                logger.warning("[sandbox-registry] destruction lease cleanup failed %s: %s",
                               sandbox_id, exc)


async def untrack(sandbox_id: str, worker_token: str) -> None:
    await get_ephemeral_state().release(_TRACK_KEY.format(sandbox_id=sandbox_id), worker_token)


async def tracked_owner(sandbox_id: str) -> Optional[str]:
    return await get_ephemeral_state().get(_TRACK_KEY.format(sandbox_id=sandbox_id))


@contextlib.asynccontextmanager
async def creation_guard() -> AsyncIterator[str]:
    """Publish renewable creation intent before the controller exposes a sandbox.

    The same claim implementation renews the intent and stops its owner on loss.
    Creation must acquire tracking ownership before this guard is released.
    """
    token = uuid.uuid4().hex
    async with claim(f"creating:{token}"):
        yield token


async def creation_pending(token: str) -> bool:
    return bool(await get_ephemeral_state().get(
        _LOCK_KEY.format(session_id=f"creating:{token}")
    ))


async def bindings():
    """Snapshot shared bindings without extending activity or their lifetime."""
    prefix = _KEY.split("{")[0]
    state = get_ephemeral_state()
    entries = []
    for key in await state.keys(prefix):
        session_id = key[len(prefix):]
        entry = await _read(session_id)
        if entry is not None:
            entries.append((session_id, entry[0], entry[1]))
    return entries

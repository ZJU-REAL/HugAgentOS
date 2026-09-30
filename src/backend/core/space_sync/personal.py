"「我的空间」的登记由文件系统驱动，不由某个工具顺手完成。"

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from core.llm.tools._myspace_confirm import OP_DELETE, OP_EDIT
from core.llm.tools.myspace_vfs import MYSPACE_LOGICAL
from core.myspace import mirror
from core.sandbox._common import myspace_cache_root
from core.space_sync.events import Change, normalize
from core.space_sync.journal import Journal
from core.space_sync.personal_policy import _INFLIGHT_BUDGET_BYTES, Ask, Budget, _split
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

_SETTLE_S = 0.05
_ERROR_BACKOFF_S = 0.5
_HANDOFF_GRACE_S = 0.05
_CONFIRM_TIMEOUT_S = 300.0


from core.myspace.registry_claims import claim as _claim
from core.myspace.registry_claims import claim_batch


def _stat_all(user_id: str, rels: list[str]) -> dict[str, Optional[mirror.MirrorEntry]]:
    "一批路径现在还在不在磁盘上（只 stat，不查库）。认领要用它给出的 mtime。"
    return {rel: mirror.mirror_entry(user_id, rel) for rel in rels}


class _Handler(FileSystemEventHandler):
    "watchdog 在自己的线程里回调，这里只负责把路径丢回事件循环。"

    def __init__(self, owner: "MySpaceRegistry") -> None:
        self._owner = owner

    def on_any_event(self, event) -> None:  # noqa: ANN001 — watchdog 的事件类型
        self._owner._on_event(event)


class MySpaceRegistry:
    "监听镜像目录，把每一次写入和删除登记回「我的空间」。"

    def __init__(self) -> None:
        self._root: Optional[Path] = None
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict[tuple[str, str], float] = {}
        self._inflight: set[tuple[str, str]] = set()
        self._deferred = {}
        self._observed = {}
        self._processing_times = {}
        self._budget: Optional[Budget] = None
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._drain: Optional[asyncio.Task] = None
        self._closed = False
        self._journal = None
        self._inbox = None
        self._subscriptions = []
        self._events = {}
        self._versions = {}
        self._user_locks = {}
        self._failures = {}
        self._task_users = {}

    async def start(self) -> None:
        root = myspace_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._journal = Journal(root.parent / "space_sync" / "personal.sqlite")
        self._loop = asyncio.get_running_loop()
        from core.space_sync.inbox import AsyncInbox

        self._inbox = AsyncInbox(self._journal, self._consume)
        self._budget = Budget(_INFLIGHT_BUDGET_BYTES)
        self._wake = asyncio.Event()
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self._root), recursive=True)
        self._observer.start()
        self._drain = asyncio.create_task(self._drain_loop())
        for key, generation, change, attempts in self._journal.pending():
            user, rel = key.split(":", 1)
            self._events[(user, rel)] = change
            self._versions[(user, rel)] = generation
            self._mark((user, rel), change.observed)
        await asyncio.to_thread(self._recover)
        from core.space_sync.personal_database import subscriptions

        self._subscriptions = subscriptions(self)
        from core.space_sync.personal_database import projection_owners

        for user in await asyncio.to_thread(projection_owners):
            self._inbox.put(user + ":", Change("refresh", ""))
        logger.info("[myspace-registry] 开始监听 %s", self._root)

    async def stop(self) -> None:
        self._closed = True
        for unsubscribe in self._subscriptions:
            unsubscribe()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            await asyncio.to_thread(observer.join, 5)
        if self._inbox:
            await self._inbox.stop()
        self._wake.set()  # 叫醒挂在事件上的去抖循环，让它看见 _closed
        if self._drain is not None:
            self._drain.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._journal:
            self._journal.close()
        logger.info("[myspace-registry] 已停止")

    def _on_event(self, event):
        if self._closed or self._loop is None or self._root is None:
            return
        change = normalize(event, self._root, mirror.SKIP_DIR_NAMES)
        if change:
            self._loop.call_soon_threadsafe(self._receive, change)

    def _receive(self, change):
        source = _split(str(self._root / change.source), self._root)
        dest = (
            _split(str(self._root / change.destination), self._root) if change.destination else None
        )
        if source and (dest is None or source[0] == dest[0]):
            key = dest or source
            converted = Change(
                change.kind, source[1], dest[1] if dest else None, change.directory, change.observed
            )
            previous = self._events.get(key)
            if previous and previous.kind == "moved" and converted.kind in ("modified", "closed"):
                converted = previous
            if self._inbox:
                self._inbox.put(":".join(key), converted)
                return
            self._events[key] = converted
            self._mark(key, change.observed)
            if dest and dest != source:
                self._mark(source, change.observed)
            if change.kind == "closed":
                self._pending[key] -= _SETTLE_S
        else:
            for key in (source, dest):
                if key:
                    self._mark(key, change.observed)

    def _consume(self, raw, generation, change):
        user, rel = raw.split(":", 1)
        key = (user, rel)
        self._events[key] = change
        self._versions[key] = generation
        self._mark(key, change.observed)
        if change.destination and change.destination != change.source:
            self._mark((user, change.source), change.observed)
        if change.kind == "closed":
            self._pending[key] -= _SETTLE_S

    def _recover(self):
        from core.space_sync.recovery import recover_personal

        recover_personal(self._root, mirror.SKIP_DIR_NAMES, self._on_event)

    def _mark(self, key: tuple[str, str], observed=None) -> None:
        self._observed[key] = observed if observed is not None else time.time()
        self._pending[key] = time.monotonic()
        self._wake.set()

    async def _drain_loop(self) -> None:
        "把安静下来的路径交出去处理。同一路径同时只处理一次。"
        while not self._closed:
            try:
                if not self._pending:
                    await self._wake.wait()
                    self._wake.clear()
                    continue
                now = time.monotonic()
                ready = [k for k, ts in self._pending.items() if now - ts >= _SETTLE_S]
                if not ready:
                    await asyncio.sleep(max(_SETTLE_S - (now - min(self._pending.values())), 0.05))
                    continue
                started = self._dispatch(ready, now)
                if not started:
                    await asyncio.sleep(0.01)  # Let the parent/inflight task complete.
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 这条循环不能倒
                logger.warning("[myspace-registry] 去抖循环出错: %s", exc)
                await asyncio.sleep(_ERROR_BACKOFF_S)

    def _dispatch(
        self, ready: list[tuple[str, str]], now: float, *, force: bool = False
    ) -> list[asyncio.Task]:
        "按用户成批派发：同一批共用一次会话查询和一次判定，不逐个文件重来。"
        by_user: dict[str, list[str]] = {}
        parent_moves = [
            (key, change)
            for key, change in self._events.items()
            if change.directory and change.kind == "moved"
        ]
        ready = sorted(
            ready,
            key=lambda key: (
                not any(key == parent for parent, change in parent_moves),
                key[1].count("/"),
            ),
        )
        for key in ready:
            if any(
                parent != key
                and parent[0] == key[0]
                and (parent not in ready or parent in self._inflight)
                and any(key[1].startswith(path + "/") for path in change.paths())
                for parent, change in parent_moves
            ):
                continue
            self._pending.pop(key, None)
            if key in self._inflight:
                self._pending[key] = now  # 上一次还没跑完，等下一轮
                continue
            self._processing_times[key] = self._observed.pop(key, time.time())
            self._inflight.add(key)
            by_user.setdefault(key[0], []).append(key[1])
        started: list[asyncio.Task] = []
        for user_id, rels in by_user.items():
            task = asyncio.create_task(self._guarded(user_id, rels, force=force))
            self._tasks.add(task)
            self._task_users[task] = user_id

            def completed(done):
                self._tasks.discard(done)
                self._task_users.pop(done, None)

            task.add_done_callback(completed)
            started.append(task)
        return started

    async def _guarded(self, user_id: str, rels: list[str], *, force: bool = False) -> None:
        try:
            lock = self._user_locks.setdefault(user_id, asyncio.Lock())
            async with lock, claim_batch():
                versions = {rel: self._versions.get((user_id, rel)) for rel in rels}
                current = rels
                if self._journal:
                    durable = await asyncio.to_thread(
                        lambda: {rel: self._journal.get(user_id + ":" + rel) for rel in rels}
                    )
                    current = [
                        rel
                        for rel in rels
                        if versions[rel] is None
                        or durable[rel]
                        and durable[rel][0] == versions[rel]
                    ]
                # Parent moves can retire a queued child key after it entered memory.
                # Its replacement owns the work; retire the old cache without replaying it.
                if current:
                    await self._process(user_id, current, force=force)
                for rel, generation in versions.items():
                    if self._journal and generation is not None:
                        self._journal.acknowledge(user_id + ":" + rel, generation)
                    if generation == self._versions.get((user_id, rel)):
                        self._events.pop((user_id, rel), None)
                        self._deferred.pop((user_id, rel), None)
                        self._failures.pop((user_id, rel), None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[myspace-registry] 本次登记未完成 user=%s count=%d error=%s",
                user_id,
                len(rels),
                type(exc).__name__,
            )
            for rel in rels:
                key = (user_id, rel)
                self._failures[key] = exc
                self._deferred[key] = self._processing_times.get(key, time.time())
                delay = 2.0
                if self._journal and key in self._versions:
                    delay = self._journal.retry(":".join(key), self._versions[key], exc)
                self._pending.setdefault(key, time.monotonic() + delay)
                self._wake.set()
        finally:
            for rel in rels:
                self._inflight.discard((user_id, rel))
                self._processing_times.pop((user_id, rel), None)

    async def _register(self, user_id: str, entry) -> None:
        "把一个文件登记进账本。只有这一步把文件内容读进内存，所以只有这一步占内存额度。"
        assert self._budget is not None
        async with self._budget.reserve(entry.size):
            if await asyncio.to_thread(mirror.register_entry, user_id=user_id, entry=entry):
                logger.info("[myspace-registry] 登记 %s user=%s", entry.rel, user_id)
            else:
                raise RuntimeError("MySpace registration did not persist the file")

    async def _process(self, user_id: str, rels: list[str], *, force: bool = False) -> None:
        "处理这个用户这一批改动。"
        refresh = "" in rels
        rels = [rel for rel in rels if rel]
        ask = Ask(user_id)
        if not force and await ask.get() is None:
            await asyncio.sleep(_HANDOFF_GRACE_S)

        from core.space_sync.personal_metadata import apply

        directories = set()
        for rel in rels:
            change = self._events.get((user_id, rel))
            if change and (change.directory or change.kind == "moved"):
                if change.kind == "created" and change.directory:
                    await asyncio.to_thread(apply, user_id, change)
                elif change.kind == "moved":
                    if await self._approved(
                        user_id=user_id,
                        ask=ask,
                        logical_path=f"{MYSPACE_LOGICAL}/{change.source}",
                        op=OP_EDIT,
                        summary="同步空间目录或移动文件",
                    ):
                        await asyncio.to_thread(apply, user_id, change)
                    elif change.kind == "moved":
                        from core.space_sync.personal_metadata import restore_move

                        await asyncio.to_thread(restore_move, user_id, change)
                if change.directory and change.kind != "deleted":
                    directories.add(rel)
        rels = [rel for rel in rels if rel not in directories]
        entries = await asyncio.to_thread(_stat_all, user_id, rels)
        writes = [(rel, e) for rel, e in entries.items() if e is not None]
        gone = [
            rel
            for rel, e in entries.items()
            if e is None
            and (
                self._events.get((user_id, rel)) is None
                or self._events[(user_id, rel)].kind in ("deleted", "moved")
            )
        ]
        from core.space_sync.content import signature

        def token(entry):
            try:
                return repr(signature(entry.path))
            except OSError:
                return repr(entry.mtime)

        won = [await _claim(user_id, rel, token(e)) for rel, e in writes]
        claimed = {rel: e for (rel, e), ok in zip(writes, won) if ok}
        if gone:
            await self._apply_deletes(user_id, gone, ask)
        if not claimed:
            if refresh:
                await self._refresh(user_id)
            return

        verdicts = await asyncio.to_thread(
            mirror.classify_claimed, user_id=user_id, entries=claimed
        )
        fresh = []
        for rel, verdict in verdicts.items():
            entry = claimed[rel]
            if verdict == mirror.VERDICT_NEW:
                fresh.append(entry)
            elif verdict == mirror.VERDICT_TOO_LARGE:
                logger.info("[myspace-registry] 超过大小上限不登记 %s/%s", user_id, rel)
            elif verdict == mirror.VERDICT_MODIFIED:
                if await self._approved(
                    user_id=user_id,
                    ask=ask,
                    logical_path=entry.logical_path,
                    op=OP_EDIT,
                    summary=f"沙盒改写了「我的空间」里已有的文件：{rel}",
                ):
                    await self._register(user_id, entry)
                else:
                    await asyncio.to_thread(mirror.restore_from_registry, user_id=user_id, rel=rel)
        if fresh:
            await asyncio.gather(*(self._register(user_id, e) for e in fresh))
        if refresh:
            await self._refresh(user_id)

    async def _refresh(self, user_id):
        from core.myspace.projection import pull_myspace_updates

        report = await asyncio.to_thread(pull_myspace_updates, user_id=user_id)
        if report.failed or report.conflicted:
            raise RuntimeError("Space projection remains pending")

    async def _apply_deletes(self, user_id: str, rels: list[str], ask: "Ask") -> None:
        "磁盘上没了的这些路径，该从账本里删的一起删掉。"
        targets = await asyncio.to_thread(mirror.classify_deletes, user_id=user_id, rels=rels)
        for rel, target in targets.items():
            if target is None:
                continue  # 没登记过的东西消失了，本来就不在用户空间里
            from core.space_sync.index import get

            baseline = await asyncio.to_thread(get, user_id, rel)
            if (
                baseline
                and target.registered
                and (
                    baseline.get("id") != target.registered.artifact_id
                    or baseline.get("key") != target.registered.storage_key
                )
            ):
                raise RuntimeError("Cloud identity changed; local deletion remains pending")
            observed = self._processing_times.get((user_id, rel), time.time())
            if (
                target.registered
                and target.registered.registered_ts is not None
                and target.registered.registered_ts > observed
            ):
                continue  # A delayed event must not delete a newly uploaded identity.
            if await asyncio.to_thread(mirror.mirror_entry, user_id, rel) is not None:
                continue
            if not await _claim(user_id, rel, f"del:{target.stamp}"):
                continue
            if not await self._approved(
                user_id=user_id,
                ask=ask,
                logical_path=f"{MYSPACE_LOGICAL}/{rel}",
                op=OP_DELETE,
                summary=f"沙盒删除了「我的空间」里的{target.kind_label}：{rel}",
            ):
                if target.registered is not None:
                    await asyncio.to_thread(
                        mirror.restore_from_registry,
                        user_id=user_id,
                        rel=rel,
                        reg=target.registered,
                    )
                continue
            if not await asyncio.to_thread(
                mirror.delete_registered, user_id=user_id, rel=rel, target=target
            ):
                raise RuntimeError("MySpace deletion did not persist")
            from core.space_sync.index import forget

            await asyncio.to_thread(forget, user_id, rel, target.folder)

    async def _approved(
        self,
        *,
        user_id: str,
        ask: "Ask",
        logical_path: str,
        op: str,
        summary: str,
    ) -> bool:
        "要不要让这次改动落进账本。问不到人、或用户的权限档已经替他答过，就直接放行。"
        chat_id = await ask.get()
        if chat_id is None:
            return True
        from core.llm.tool_permissions import preset_answers_for_user

        if await asyncio.to_thread(preset_answers_for_user, user_id, op=op):
            return True
        from core.llm.tools import _myspace_confirm as mc

        res = await mc.gate(
            chat_id=chat_id,
            op=op,
            logical_path=logical_path,
            interactive=True,
            summary=summary,
            timeout=_CONFIRM_TIMEOUT_S,
        )
        return res is None

    async def flush(self, user_id: str, *, timeout: float = 5.0) -> None:
        "Wait for this owner's complete queue, including parent/child dependencies."
        from core.space_sync.personal_barrier import flush

        await flush(self, user_id, timeout)

"""One output collector per command; no model-driven polling."""

import asyncio
import logging
import time
import uuid
from functools import wraps

from core.sandbox.protocol import ProcessRequest
from core.sandbox.providers.opensandbox.recovery import (
    execution_reference,
    recover_execution,
    release_execution,
)
from core.sandbox.commands import store

logger = logging.getLogger(__name__)
LAUNCH_RENEW_SECONDS = 10


def service_loop(method):
    """Keep command collectors on their service loop across agent worker threads."""

    @wraps(method)
    async def invoke(self, *args, **kwargs):
        current = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = current
        if self.loop is current:
            return await method(self, *args, **kwargs)
        if not self.loop.is_running():
            raise RuntimeError("Command service loop is not running")
        future = asyncio.run_coroutine_threadsafe(method(self, *args, **kwargs), self.loop)
        return await asyncio.wrap_future(future)

    return invoke


class CommandRuntime:
    def __init__(self):
        self.loop = None
        self.owner = uuid.uuid4().hex
        self.tasks = {}
        self.providers = {}
        self._locks = {}
        self._heartbeat = None

    def _ensure_heartbeat(self):
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._renew_handles())

    async def _renew_handles(self):
        while self.providers:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(store.renew_owned, list(self.providers), self.owner),
                    timeout=store.LEASE_SECONDS / 3,
                )
            except Exception:
                logger.exception("Command handle renewal failed")
            await asyncio.sleep(store.LEASE_SECONDS / 3)

    def bind_loop(self):
        """Initialize every worker before a temporary agent thread can call it."""
        current = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not current:
            raise RuntimeError("Command runtime already belongs to another event loop")
        self.loop = current

    @service_loop
    async def start(
        self, provider, req, *, chat_id, source_run_id, background=False, yield_time_ms=60000
    ):
        row = await asyncio.to_thread(store.create, req, chat_id, source_run_id, self.owner)
        tid = row["task_id"]
        result = None
        try:
            result = await self._launch(
                tid, provider, req, yield_time_ms=250 if background else yield_time_ms
            )
            return await self._attach(tid, provider, req, result)
        except BaseException as exc:
            if result and result.get("status") == "running" and tid not in self.providers:
                # Preserve control even if persistence or the first stop RPC fails.
                self.providers[tid] = provider, req, result["session_id"]
                self._ensure_heartbeat()
                await self._interrupt(tid)
                self.providers.pop(tid, None)
                self._locks.pop(tid, None)
            try:
                await asyncio.to_thread(
                    store.finish, tid, "unknown", "Launch state unknown; do not rerun: " + str(exc)
                )
            except LookupError:
                pass  # A hard-deleted chat already removed the task.
            raise

    async def _launch(self, tid, provider, req, *, yield_time_ms):
        async def renew():
            while True:
                await asyncio.sleep(LAUNCH_RENEW_SECONDS)
                await asyncio.to_thread(store.renew_launch, tid, self.owner)

        launch = asyncio.create_task(provider.start_process(req, yield_time_ms=yield_time_ms))
        lease = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((launch, lease), return_when=asyncio.FIRST_COMPLETED)
            if launch in done:
                return await launch
            await lease  # Lease loss cancels the pending launch; never restart it.
            raise RuntimeError("Command launch lease ended unexpectedly")
        finally:
            for task in (launch, lease):
                if not task.done():
                    task.cancel()
            await asyncio.gather(launch, lease, return_exceptions=True)

    async def _attach(self, tid, provider, req, result):
        reference = None
        if result.get("status") == "running":
            reference = await execution_reference(provider, result["session_id"], req)
            if reference is not None:
                reference["cursor"] = result["execution_cursor"]
        row = await asyncio.to_thread(store.attach, tid, self.owner, result, reference)
        payload = await asyncio.to_thread(
            store.read, tid, req.user_id, req.session_id, consume_notice=False
        )
        if row["status"] == "running":
            self.providers[tid] = provider, req, result["session_id"]
            self._ensure_heartbeat()
            self.tasks[tid] = asyncio.create_task(self._monitor(tid, row))
            payload["next"] = (
                "Command is managed in the background. Completion will notify this chat. Do independent work; do not poll or restart it. If no independent work remains, end this response once; completion will resume this chat."
            )
        return payload

    @service_loop
    async def adopt(self, provider, process_id, *, user_id, session_id, chat_id, source_run_id):
        if process_id.startswith("cmd_"):
            await asyncio.to_thread(store.owned, process_id, user_id, session_id)
            return await asyncio.to_thread(
                store.read, process_id, user_id, session_id, consume_notice=False
            )
        req = ProcessRequest(
            script_content="[adopted command]",
            script_name="_managed.sh",
            language="bash",
            session_id=session_id,
            user_id=user_id,
        )
        row = await asyncio.to_thread(
            store.create, req, chat_id, source_run_id, self.owner, process_id
        )
        if not row["created"]:
            return await asyncio.to_thread(
                store.read, row["task_id"], user_id, session_id, consume_notice=False
            )
        try:
            result = await provider.write_stdin(
                process_id,
                sandbox_session_id=session_id,
                user_id=user_id,
                chars="",
                yield_time_ms=0,
            )
            return await self._attach(row["task_id"], provider, req, result)
        except BaseException:
            await asyncio.to_thread(
                store.finish, row["task_id"], "unknown", "Process adoption failed"
            )
            raise

    async def _monitor(self, tid, row):
        provider, req, pid = self.providers[tid]
        last_touch = 0
        failures = 0
        try:
            while True:
                try:
                    if not await asyncio.to_thread(store.chat_available, tid):
                        result = await self._interrupt(tid)
                        try:
                            await asyncio.to_thread(
                                store.control_result,
                                tid,
                                self.owner,
                                "cancelled",
                                "Conversation removed",
                                True,
                                result,
                            )
                        except LookupError:
                            pass  # Physical deletion removed the row; the owned process was stopped.
                        break
                    row = await asyncio.to_thread(store.snapshot, tid)
                    if row["status"] not in store.LIVE or row["owner"] != self.owner:
                        if row["execution_ref"]:
                            await release_execution(provider, pid, req)
                        else:
                            await self._interrupt(tid)
                        break
                    if row["cancel_requested"]:
                        result = await self._interrupt(tid)
                        await asyncio.to_thread(
                            store.control_result, tid, self.owner, "cancelled", "", True, result
                        )
                        break
                    if row["deadline_at"] and time.time() >= row["deadline_at"]:
                        result = await self._interrupt(tid)
                        await asyncio.to_thread(
                            store.control_result,
                            tid,
                            self.owner,
                            "failed",
                            "Command execution deadline exceeded",
                            False,
                            result,
                        )
                        break
                    async with self._locks.setdefault(tid, asyncio.Lock()):
                        result = await provider.write_stdin(
                            pid,
                            sandbox_session_id=req.session_id,
                            user_id=req.user_id,
                            chars="",
                            yield_time_ms=250,
                        )
                        row = await asyncio.to_thread(store.collect, tid, self.owner, result)
                    if row["status"] not in store.LIVE:
                        break
                    if time.monotonic() - last_touch >= 10:
                        await provider.touch_session(req.session_id)
                        last_touch = time.monotonic()
                    failures = 0
                    await asyncio.sleep(0.5)
                except Exception:
                    failures += 1
                    logger.warning("Command observation failed task=%s attempt=%s",
                                   tid, failures, exc_info=True)
                    if failures >= 3:
                        if row["execution_ref"]:
                            # Durable recovery can retry without rerunning the command.
                            await release_execution(provider, pid, req)
                            await asyncio.to_thread(store.release_lease, tid, self.owner)
                            break
                        # A nonrecoverable local process must be stopped, never
                        # reported terminal merely because observation failed.
                        result = await self._interrupt(tid)
                        await asyncio.to_thread(
                            store.control_result, tid, self.owner, "failed",
                            "Command observation failed", False, result,
                        )
                        break
                    await asyncio.sleep(2 ** (failures - 1))
        except asyncio.CancelledError:
            if row["execution_ref"]:
                await release_execution(provider, pid, req)
                self.providers.pop(tid, None)
                self._locks.pop(tid, None)
                await asyncio.to_thread(store.release_lease, tid, self.owner)
            else:
                result = await self._interrupt(tid)
                self.providers.pop(tid, None)
                self._locks.pop(tid, None)
                await asyncio.to_thread(
                    store.control_result,
                    tid,
                    self.owner,
                    "cancelled",
                    "Command owner stopped",
                    True,
                    result,
                )
            raise
        else:
            self.providers.pop(tid, None)
            self._locks.pop(tid, None)
        finally:
            self.tasks.pop(tid, None)

    async def _interrupt(self, tid):
        provider, req, pid = self.providers[tid]
        async with self._locks.setdefault(tid, asyncio.Lock()):
            result = await provider.write_stdin(
                pid,
                sandbox_session_id=req.session_id,
                user_id=req.user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            while result.get("status") == "running":
                try:
                    await asyncio.to_thread(store.collect, tid, self.owner, result)
                except Exception:
                    logger.warning("Command stop bookkeeping deferred task=%s", tid)
                next_result = await provider.write_stdin(
                    pid,
                    sandbox_session_id=req.session_id,
                    user_id=req.user_id,
                    chars="",
                    yield_time_ms=1000,
                )
                result = next_result
            if result.get("status") != "exited":
                raise RuntimeError("Process stop not confirmed")
            return result

    async def cancel(self, tid, user_id, session_id):
        await asyncio.to_thread(store.owned, tid, user_id, session_id)
        row = await asyncio.to_thread(store.snapshot, tid)
        if row["status"] in store.TERMINAL:
            return
        await asyncio.to_thread(store.request_cancel, tid)
        await self.wait(tid, user_id, session_id, 3000)

    async def wait(self, tid, user_id, session_id, wait_ms=0):
        await asyncio.to_thread(store.owned, tid, user_id, session_id)
        deadline = time.monotonic() + max(0, min(wait_ms, 300000)) / 1000
        while time.monotonic() < deadline:
            if (await asyncio.to_thread(store.snapshot, tid))["status"] in store.TERMINAL:
                break
            await asyncio.sleep(0.05)

    @service_loop
    async def recover(self, provider):
        # Failed local stops remain owned and are retried by this worker's
        # existing recovery supervisor before any no-reference classification.
        for tid in list(self.providers):
            if tid not in self.tasks:
                self.tasks[tid] = asyncio.create_task(self._stop_retained(tid))
        for row in await asyncio.to_thread(store.pending_recovery):
            tid = row["task_id"]
            if tid in self.tasks and not self.tasks[tid].done():
                continue
            if not await asyncio.to_thread(store.claim, tid, self.owner):
                continue
            if not row["execution_ref"]:
                await asyncio.to_thread(
                    store.finish,
                    tid,
                    "unknown",
                    "No recoverable execution reference; original command will not be rerun",
                    owner=self.owner,
                )
                continue
            req = ProcessRequest(
                script_content="",
                script_name="_managed.sh",
                language="bash",
                session_id=row["sandbox_session_id"],
                user_id=row["user_id"],
            )
            result = None
            try:
                result = await recover_execution(provider, row["execution_ref"], req)
                ref = {**row["execution_ref"], "cursor": result["execution_cursor"]}
                recovered = await asyncio.to_thread(
                    store.save_recovery, tid, self.owner, result, ref
                )
                if recovered["status"] == "running":
                    self.providers[tid] = provider, req, result["session_id"]
                    self._ensure_heartbeat()
                    self.tasks[tid] = asyncio.create_task(self._monitor(tid, recovered))
            except BaseException as exc:
                if result and result.get("status") == "running":
                    await release_execution(provider, result["session_id"], req)
                await asyncio.to_thread(store.release_lease, tid, self.owner)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning("Command recovery deferred task=%s", tid, exc_info=True)

    async def _stop_retained(self, tid):
        try:
            try:
                row = await asyncio.to_thread(store.snapshot, tid)
            except LookupError:
                row = None
            if row and row["execution_ref"]:
                provider, req, pid = self.providers[tid]
                await release_execution(provider, pid, req)
                self.providers.pop(tid, None)
                self._locks.pop(tid, None)
                await asyncio.to_thread(store.release_lease, tid, self.owner)
                return
            result = await self._interrupt(tid)
            self.providers.pop(tid, None)
            self._locks.pop(tid, None)
            try:
                await asyncio.to_thread(
                    store.control_result, tid, self.owner, "failed",
                    "Command observation failed", False, result,
                )
            except LookupError:
                pass
        except Exception:
            logger.exception("Command stop requires another recovery pass task=%s", tid)
        finally:
            self.tasks.pop(tid, None)

    @service_loop
    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first instruction cannot run its finally.
        for tid, (provider, req, pid) in list(self.providers.items()):
            try:
                await self._shutdown_handle(tid, provider, req, pid)
            except Exception as exc:
                results.append(exc)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result
        self.tasks.clear()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None
        self.loop = None

    async def _shutdown_handle(self, tid, provider, req, pid):
        try:
            row = await asyncio.to_thread(store.snapshot, tid)
        except LookupError:
            await self._interrupt(tid)
            self.providers.pop(tid, None)
            self._locks.pop(tid, None)
            return
        if row["execution_ref"]:
            await release_execution(provider, pid, req)
            await asyncio.to_thread(store.release_lease, tid, self.owner)
        else:
            result = await self._interrupt(tid)
            await asyncio.to_thread(
                store.control_result,
                tid,
                self.owner,
                "cancelled",
                "Command owner stopped",
                True,
                result,
            )
        self.providers.pop(tid, None)
        self._locks.pop(tid, None)


runtime = CommandRuntime()

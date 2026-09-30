"""Keep filesystem callbacks memory-only; durably commit batches off the asyncio loop."""

import asyncio


class AsyncInbox:
    def __init__(self, journal, consume):
        self.journal = journal
        self.consume = consume
        self.queue = asyncio.Queue()
        self.worker = asyncio.create_task(self._run())

    def put(self, key, change):
        self.queue.put_nowait((key, change, 0))

    async def _run(self):
        while True:
            first = await self.queue.get()
            entries = [first]
            while len(entries) < 256:
                try:
                    entries.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            while True:
                try:
                    saved = await asyncio.to_thread(self.journal.put_many, entries)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(1)
            for key, (generation, change) in saved.items():
                self.consume(key, generation, change)
            for _ in entries:
                self.queue.task_done()

    async def flush(self):
        await asyncio.wait_for(self.queue.join(), timeout=5)

    async def stop(self):
        try:
            await self.flush()
        except TimeoutError:
            import logging

            logging.getLogger(__name__).error(
                "Space journal unavailable at shutdown; filesystem recovery required"
            )
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

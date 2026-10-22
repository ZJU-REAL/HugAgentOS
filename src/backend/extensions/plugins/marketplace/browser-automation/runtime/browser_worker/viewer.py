"""Reliable control packets and one replaceable frame per viewer."""
import asyncio

class Viewer:
    def __init__(self):
        self.controls = asyncio.Queue(maxsize=32)
        self.frame = None
        self.ready = asyncio.Event()
        self.overflow = False

    def put(self, data, frame=False):
        if frame:
            self.frame = data
        elif self.controls.full():
            self.overflow = True
        else:
            self.controls.put_nowait(data)
        self.ready.set()

    async def get(self):
        await self.ready.wait()
        if self.overflow:
            raise ValueError("slow_consumer")
        if not self.controls.empty():
            data = self.controls.get_nowait()
        else:
            data, self.frame = self.frame, None
        if self.controls.empty() and self.frame is None:
            self.ready.clear()
        return data

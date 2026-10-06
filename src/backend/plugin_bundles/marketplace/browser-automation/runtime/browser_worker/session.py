"""One worker owns one browser context, bounded viewers and control state."""
import asyncio
import base64
import time
import os
import logging
from playwright.async_api import async_playwright
from .network import NetworkPolicy
from .egress import BrowserEgress
from .protocol import packet, VIEWPORT_LIMITS
from .viewer import Viewer

class BrowserSession:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.page_registration_lock = asyncio.Lock()
        self.selection_lock = asyncio.Lock()
        self.pages = {}
        self.titles = {}
        self.title_tasks = {}
        self.active_tab = ""
        self.next_tab = 0
        self.viewport_revision = 0
        self.epoch = 0
        self.controller = "agent"
        self.connection_id = ""
        self.private = False
        self.last_active = time.monotonic()
        self.viewers = set()
        self.latest_frame = None
        self.cdp = None
        self.frame_id = 0
        self.dialogs = {}
        self.downloads = {}
        self.choosers = {}
        self.results = {}
        self.closed = False
        self.tasks = set()

    def spawn(self, coro):
        if self.closed:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)

    def task_finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None and not self.closed:
            logging.getLogger(__name__).warning("Browser event failed: %s", type(task.exception()).__name__)
            self.broadcast(packet({"type": "stream_error", "error": "browser_event_failed"}))

    async def start(self):
        self.playwright = await async_playwright().start()
        self.policy = NetworkPolicy(self.config.get("allowed_hosts", []), self.config.get("allow_private", False), self.config.get("dns_resolver_url", ""))
        self.egress = BrowserEgress(self.policy)
        proxy = await self.egress.start()
        options = {"headless": True, "ignore_default_args": ["--hide-scrollbars"], "chromium_sandbox": self.config.get("chromium_sandbox", True) and (not hasattr(os, "geteuid") or os.geteuid() != 0),
                   "proxy": {"server": proxy}, "args": ["--proxy-bypass-list=<-loopback>", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]}
        if self.config.get("executable"):
            options["executable_path"] = self.config["executable"]
        self.browser = await self.playwright.chromium.launch(**options)
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800}, accept_downloads=True,
            service_workers="block", storage_state=self.config.get("checkpoint"),
        )
        await self.context.route("**/*", self.policy.route)
        await self.context.route_web_socket("**/*", self.policy.websocket)
        self.context.on("page", lambda page: self.spawn(self.add_page(page)))
        await self.add_page(await self.context.new_page())
        self.spawn(self.refresh_titles())

    async def add_page(self, page):
        async with self.page_registration_lock:
            await self.register_page(page)

    async def register_page(self, page):
        if page.is_closed() or page in self.pages.values():
            return
        if len(self.pages) >= int(self.config.get("max_tabs", 12)):
            await page.close()
            return
        self.next_tab += 1
        tab = str(self.next_tab)
        self.pages[tab] = page
        page.on("close", lambda: self.spawn(self.page_closed(tab)))
        page.on("framenavigated", lambda frame: self.spawn(self.publish_state()) if frame == page.main_frame else None)
        page.on("domcontentloaded", lambda: self.schedule_title(tab, page))
        page.on("load", lambda: self.schedule_title(tab, page))
        self.schedule_title(tab, page)
        page.on("dialog", lambda dialog: self.spawn(self.dialog_open(tab, dialog)))
        page.on("download", lambda download: self.spawn(self.download_open(tab, download)))
        page.on("filechooser", lambda chooser: self.spawn(self.chooser_open(tab, chooser)))
        await self.select(tab)
        await self.publish_state()

    async def select(self, tab):
        async with self.selection_lock:
            await self.select_page(tab)

    async def select_page(self, tab):
        if tab not in self.pages:
            raise ValueError("unknown_tab")
        if self.cdp and self.active_tab in self.pages and not self.pages[self.active_tab].is_closed():
            await self.cdp.detach()
        self.active_tab = tab
        self.viewport_revision += 1
        self.latest_frame = None
        self.cdp = await self.context.new_cdp_session(self.pages[tab])
        await self.cdp.send("Page.bringToFront")
        cdp, revision = self.cdp, self.viewport_revision
        self.cdp.on("Page.screencastFrame", lambda event: self.spawn(self.frame(event, cdp, tab, revision)))
        await self.publish_state()
        if self.viewers:
            await self.start_stream()

    async def start_stream(self):
        tab, revision, cdp = self.active_tab, self.viewport_revision, self.cdp
        page = self.pages.get(tab)
        if not page or not cdp or self.closed:
            return
        viewport = page.viewport_size
        try:
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 75, "maxWidth": 1600, "maxHeight": 1200})
            data = await page.screenshot(type="jpeg", quality=75)
        except Exception:
            if cdp == self.cdp and tab == self.active_tab and not self.closed:
                raise
            return
        # A new viewer's screenshot can finish after a popup switches targets.
        if self.closed or cdp != self.cdp or tab != self.active_tab or revision != self.viewport_revision or viewport != page.viewport_size:
            return
        self.frame_id += 1
        self.latest_frame = packet({"type": "frame", "tab_id": tab, "frame_id": self.frame_id, "viewport_revision": revision, "viewport": viewport}, data)
        self.broadcast(self.latest_frame, frame=True)

    async def frame(self, event, cdp, tab, revision):
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            if tab != self.active_tab or revision != self.viewport_revision or self.closed:
                return
            self.frame_id += 1
            self.latest_frame = packet({"type": "frame", "tab_id": tab, "frame_id": self.frame_id, "viewport_revision": revision, "viewport": self.pages[tab].viewport_size}, base64.b64decode(event["data"]))
            self.broadcast(self.latest_frame, frame=True)
        except Exception:
            # Detaching a target races its last frame callback.
            if tab == self.active_tab and not self.closed:
                self.broadcast(packet({"type": "stream_error", "error": "frame_unavailable"}))

    def broadcast(self, data, frame=False):
        for viewer in tuple(self.viewers):
            viewer.put(data, frame)

    async def state(self):
        tabs = []
        for tab, page in list(self.pages.items()):
            if not page.is_closed():
                tabs.append({"id": tab, "url": page.url, "title": self.titles.get(tab, "")})
        return {"type": "state", "tabs": tabs, "tab_limit": int(self.config.get("max_tabs", 12)), "active_tab": self.active_tab, "epoch": self.epoch, "controller": self.controller, "connection_id": self.connection_id, "private": self.private, "viewport_revision": self.viewport_revision, "viewport_limits": VIEWPORT_LIMITS, "dialogs": [{"tab_id": tab, "kind": dialog.type, "message": dialog.message} for tab, dialog in self.dialogs.items()], "downloads": [{"id": key, "name": value.suggested_filename} for key, value in self.downloads.items()], "filechoosers": list(self.choosers), "closed": self.closed}

    def schedule_title(self, tab, page):
        if self.closed or tab in self.title_tasks or tab in self.dialogs:
            return
        task = asyncio.create_task(self.refresh_title(tab, page))
        self.title_tasks[tab] = task
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)

    async def refresh_title(self, tab, page):
        try:
            try:
                title = await asyncio.wait_for(page.title(), 1)
            except Exception:
                # Navigation, dialogs or busy renderers keep the last known title.
                return
            if not self.closed and self.pages.get(tab) == page and self.titles.get(tab) != title:
                self.titles[tab] = title
                await self.publish_state()
        finally:
            if self.title_tasks.get(tab) == asyncio.current_task():
                self.title_tasks.pop(tab, None)

    async def refresh_titles(self):
        # Load events refresh promptly; this bounded fallback catches script title changes.
        while not self.closed:
            for tab, page in list(self.pages.items()):
                if not page.is_closed():
                    self.schedule_title(tab, page)
            await asyncio.sleep(1)

    async def publish_state(self):
        if not self.closed:
            self.broadcast(packet(await self.state()))

    async def subscribe(self):
        if len(self.viewers) >= 8:
            raise ValueError("too_many_viewers")
        queue = Viewer()
        self.viewers.add(queue)
        try:
            queue.put(packet(await self.state()))
            if self.active_tab:
                await self.start_stream()
            while not self.closed:
                try:
                    yield await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    yield packet({"type": "heartbeat"})
                except ValueError:
                    yield packet({"type": "stream_error", "error": "slow_consumer"})
                    break
        finally:
            self.viewers.discard(queue)
            if not self.viewers and self.cdp and not self.closed:
                await self.cdp.send("Page.stopScreencast")

    async def dialog_open(self, tab, dialog):
        self.dialogs[tab] = dialog
        self.broadcast(packet({"type": "dialog", "tab_id": tab, "kind": dialog.type, "message": dialog.message}))
        await self.publish_state()

    async def download_open(self, tab, download):
        key = str(len(self.downloads) + 1)
        self.downloads[key] = download
        await self.publish_state()

    async def chooser_open(self, tab, chooser):
        self.choosers[tab] = chooser
        await self.publish_state()

    async def page_closed(self, tab):
        async with self.page_registration_lock:
            await self.remove_page(tab)

    async def remove_page(self, tab):
        if self.closed or tab not in self.pages:
            return
        index = list(self.pages).index(tab)
        self.pages.pop(tab)
        self.titles.pop(tab, None)
        title_task = self.title_tasks.pop(tab, None)
        if title_task:
            title_task.cancel()
        self.dialogs.pop(tab, None)
        self.choosers.pop(tab, None)
        if tab == self.active_tab:
            self.active_tab = ""
            if self.pages:
                remaining = list(self.pages)
                await self.select(remaining[min(index, len(remaining) - 1)])
            else:
                self.cdp, self.latest_frame = None, None
                self.viewport_revision += 1
        await self.publish_state()

    async def close(self):
        self.closed = True
        self.broadcast(packet({"type": "closed"}))
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.title_tasks.clear()
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()
        await self.egress.close()

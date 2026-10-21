"""Exercise real SDK HTTP routing, rather than only endpoint attributes."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


class DirectExecdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            if "/command/status/" in request.url.path:
                return httpx.Response(200, json={"id": "cmd", "running": False, "exit_code": 0})
            if request.url.path.endswith("/logs"):
                return httpx.Response(200, text="done", headers={"EXECD-COMMANDS-TAIL-CURSOR": "4"})
            return httpx.Response(200, text="ok")

        self.config = ConnectionConfig(
            domain="http://controller:8080",
            transport=httpx.MockTransport(respond),
            headers={"X-Test-Identity": "preserved"},
        )
        factory = AdapterFactory(self.config)
        endpoint = SandboxEndpoint(
            endpoint="controller:8080/sandboxes/test/proxy/44772",
            headers={"X-Test-Endpoint": "preserved"},
        )
        self.box = SimpleNamespace(
            id="test",
            _command_service=factory.create_command_service(endpoint),
            _filesystem_service=factory.create_filesystem_service(endpoint),
            _health_service=factory.create_health_service(endpoint),
            _metrics_service=factory.create_metrics_service(endpoint),
            _isolated_service=getattr(factory, "create_isolated_session_service", lambda ep: None)(
                endpoint
            ),
        )
        self.box.commands = self.box._command_service
        self.box.files = self.box._filesystem_service
        self.provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
        self.provider._probe_direct_execd = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        seen = set()
        for service in (
            self.box.commands,
            self.box.files,
            self.box._health_service,
            self.box._metrics_service,
            self.box._isolated_service,
        ):
            for name in ("_httpx_client", "_sse_client"):
                client = getattr(service, name, None)
                if client and id(client) not in seen:
                    seen.add(id(client))
                    await client.aclose()

    async def test_status_and_logs_use_direct_http_with_existing_headers(self):
        self.assertTrue(await self.provider._repoint_execd_direct(self.box))
        status = await self.box.commands.get_command_status("cmd")
        logs = await self.box.commands.get_background_command_logs("cmd", cursor=0)
        self.assertFalse(status.running)
        self.assertEqual(logs.content, "done")
        self.assertEqual([r.url.host for r in self.requests], ["sandbox-test"] * 2)
        for request in self.requests:
            self.assertNotIn("/proxy/", request.url.path)
            self.assertEqual(request.headers["X-Test-Identity"], "preserved")
            self.assertEqual(request.headers["X-Test-Endpoint"], "preserved")

    async def test_file_and_interrupt_requests_also_use_direct(self):
        self.assertTrue(await self.provider._repoint_execd_direct(self.box))
        await self.box.files.read_file("/workspace/test.txt")
        await self.box.commands.interrupt("cmd")
        await self.box._health_service.ping("test")
        self.assertGreaterEqual(len(self.requests), 3)
        self.assertTrue(all(r.url.host == "sandbox-test" for r in self.requests))

    async def test_unsupported_sdk_keeps_every_route_on_proxy(self):
        original = self.box.commands.execd_endpoint.endpoint
        # An SDK layout change must not leave only commands switched to direct.
        self.box._health_service._client = object()
        self.assertFalse(await self.provider._repoint_execd_direct(self.box))
        self.assertEqual(self.box.commands.execd_endpoint.endpoint, original)
        await self.box.commands.get_command_status("cmd")
        self.assertEqual(self.requests[-1].url.host, "controller")

    async def test_missing_endpoint_keeps_every_route_on_proxy(self):
        original = self.box.commands.execd_endpoint.endpoint
        del self.box._health_service.execd_endpoint
        self.assertFalse(await self.provider._repoint_execd_direct(self.box))
        self.assertEqual(self.box.commands.execd_endpoint.endpoint, original)
        await self.box.commands.get_command_status("cmd")
        self.assertEqual(self.requests[-1].url.host, "controller")

    async def test_disabled_direct_never_probes(self):
        import core.sandbox.providers.opensandbox.creation as runtime

        with patch.object(
            runtime,
            "settings",
            SimpleNamespace(sandbox=SimpleNamespace(opensandbox_direct_execd_enabled=False)),
        ):
            self.assertFalse(await self.provider._repoint_execd_direct(self.box))
        self.provider._probe_direct_execd.assert_not_awaited()

    async def test_unreachable_direct_keeps_working_proxy(self):
        self.provider._probe_direct_execd.return_value = False
        self.assertFalse(await self.provider._repoint_execd_direct(self.box))
        await self.box.commands.get_command_status("cmd")
        self.assertEqual(self.requests[-1].url.host, "controller")


if __name__ == "__main__":
    unittest.main()

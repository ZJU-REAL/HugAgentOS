"""Contract tests against the pinned controller source; no live service mutation."""

import ast
import importlib.util
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
import httpx
from starlette.concurrency import run_in_threadpool
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

PATCH_PATH = (
    Path(os.environ["OPENSANDBOX_PATCH_SCRIPT"])
    if "OPENSANDBOX_PATCH_SCRIPT" in os.environ
    else Path(__file__).resolve().parents[4] / "docker/patch_opensandbox_server.py"
)
ROOT = PATCH_PATH.parent.parent
spec = importlib.util.spec_from_file_location("controller_patch", PATCH_PATH)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
SOURCE = Path(
    os.environ.get("OPENSANDBOX_TEST_SOURCE", ROOT / ".git/sandbox-controller-perf-source")
)


def extract(source, name, namespace):
    tree = ast.parse(source)
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    node.decorator_list = []
    node.returns = None
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, "<controller>", "exec"), namespace)
    return namespace[name]


@unittest.skipUnless(SOURCE.is_dir(), "Set OPENSANDBOX_TEST_SOURCE to the pinned server source")
class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_does_not_block_health(self):
        # Set BASELINE=1 to prove the same regression against unpatched code.
        source = (
            (SOURCE / "api/lifecycle.py").read_text()
            if os.getenv("BASELINE")
            else patch.patched_sources(SOURCE)["api/lifecycle.py"]
        )

        def slow_lookup(sandbox_id):
            time.sleep(0.25)
            return {}

        fn = extract(
            source,
            "get_sandbox",
            {
                "Header": lambda *a, **k: None,
                "Optional": __import__("typing").Optional,
                "Sandbox": dict,
                "sandbox_service": SimpleNamespace(get_sandbox=slow_lookup),
                "run_in_threadpool": run_in_threadpool,
            },
        )

        async def slow(request):
            await fn("test")
            return PlainTextResponse("done")

        async def health(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/sandbox", slow), Route("/health", health)])
        import asyncio

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            start = time.perf_counter()
            pending = asyncio.create_task(client.get("/sandbox"))
            await asyncio.sleep(0.02)
            await client.get("/health")
            health_elapsed = time.perf_counter() - start
            await pending
        self.assertLess(health_elapsed, 0.15)

    async def test_proxy_lookup_does_not_block_health(self):
        source = (
            (SOURCE / "api/proxy.py").read_text()
            if os.getenv("BASELINE")
            else patch.patched_sources(SOURCE)["api/proxy.py"]
        )

        def slow_endpoint(*args, **kwargs):
            time.sleep(0.25)
            raise HTTPException(404, "test sandbox absent")

        fn = extract(
            source,
            "_proxy_http_request",
            {
                "lifecycle": SimpleNamespace(
                    sandbox_service=SimpleNamespace(get_endpoint=slow_endpoint)
                ),
                "run_in_threadpool": run_in_threadpool,
                "_schedule_proxy_renew": lambda *args: None,
            },
        )

        async def proxy(request):
            await fn(request, "test", 44772, "command")

        async def health(request):
            return PlainTextResponse("ok")

        import asyncio

        app = Starlette(routes=[Route("/proxy", proxy), Route("/health", health)])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            start = time.perf_counter()
            pending = asyncio.create_task(client.get("/proxy"))
            await asyncio.sleep(0.02)
            await client.get("/health")
            health_elapsed = time.perf_counter() - start
            response = await pending
        self.assertEqual(response.status_code, 404)
        self.assertLess(health_elapsed, 0.15)

    def test_lookup_uses_inspect_and_checks_label(self):
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]

        class DockerError(Exception):
            pass

        class NotFound(DockerError):
            pass

        errors = SimpleNamespace(CONTAINER_QUERY_FAILED="query", SANDBOX_NOT_FOUND="missing")
        lookup = extract(
            source,
            "_get_container_by_sandbox_id",
            {
                "NotFound": NotFound,
                "DockerException": DockerError,
                "HTTPException": HTTPException,
                "status": SimpleNamespace(
                    HTTP_500_INTERNAL_SERVER_ERROR=500, HTTP_404_NOT_FOUND=404
                ),
                "SandboxErrorCodes": errors,
                "SANDBOX_ID_LABEL": "opensandbox.io/id",
                "ensure_network": lambda *args: None,
            },
        )
        seen = []
        box = SimpleNamespace(attrs={"Config": {"Labels": {"opensandbox.io/id": "test"}}})

        def get(name):
            seen.append(name)
            return box

        service = SimpleNamespace(
            docker_client=SimpleNamespace(containers=SimpleNamespace(get=get))
        )
        self.assertIs(lookup(service, "test"), box)
        self.assertEqual(seen, ["sandbox-test"])
        box.attrs["Config"]["Labels"] = {}
        with self.assertRaises(HTTPException) as mismatch:
            lookup(service, "test")
        self.assertEqual(mismatch.exception.status_code, 404)

        def missing(name):
            raise NotFound()

        service.docker_client.containers.get = missing
        with self.assertRaises(HTTPException) as missing_result:
            lookup(service, "test")
        self.assertEqual(missing_result.exception.status_code, 404)

        def failed(name):
            raise DockerError("unavailable")

        service.docker_client.containers.get = failed
        with self.assertRaises(HTTPException) as failure:
            lookup(service, "test")
        self.assertEqual(failure.exception.status_code, 500)

    def test_patch_rejects_upstream_drift_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in patch.EXPECTED:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((SOURCE / name).read_bytes())
            target.write_text("changed upstream")
            before = (root / "api/lifecycle.py").read_bytes()
            with self.assertRaises(ValueError):
                patch.apply(root)
            self.assertEqual((root / "api/lifecycle.py").read_bytes(), before)


class IsolatedNetworkTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock, patch as mock_patch
        path = Path(os.environ.get("OPENSANDBOX_NETWORK_MODULE", ROOT / "docker/opensandbox_networks.py"))
        spec = importlib.util.spec_from_file_location("isolated_networks", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.environment = mock_patch.dict(os.environ, {"HOSTNAME": "controller"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.controller = SimpleNamespace(id="controller", attrs={"Config": {"Labels": {
            "com.docker.compose.project": "test", "com.docker.compose.service": "opensandbox"}}})
        self.backend = SimpleNamespace(id="backend", attrs={"Config": {"Labels": {
            "com.docker.compose.project": "test", "com.docker.compose.service": "backend"}}})
        boxes = {"controller": self.controller, "backend": self.backend}
        self.boxes = boxes
        self.network = Mock()
        self.network.name = "test-sandbox-one"
        self.network.attrs = {"Labels": {"hugagent.project": "test", "hugagent.sandbox": "one"}, "Containers": {}}
        self.network.connect.side_effect = lambda box, **kw: self.network.attrs["Containers"].update({box.id: {}})
        self.network.disconnect.side_effect = lambda box: self.network.attrs["Containers"].pop(box.id)
        self.service = SimpleNamespace(docker_client=SimpleNamespace(
            containers=SimpleNamespace(get=boxes.__getitem__, list=lambda **kw: [self.backend]),
            networks=SimpleNamespace(create=Mock(return_value=self.network), get=lambda name: self.network),
        ))

    def test_create_and_remove_only_owned_network(self):
        self.assertEqual(self.module.create_network(self.service, "one"), self.network.name)
        self.assertEqual(set(self.network.attrs["Containers"]), {"controller", "backend"})
        self.module.remove_network(self.service, "one")
        self.assertEqual(self.network.attrs["Containers"], {})
        self.network.remove.assert_called_once()

    def test_reconnect_backend_and_reject_unexpected_attachments(self):
        box = SimpleNamespace(attrs={"Config": {"Labels": {"hugagent.sandbox-network": self.network.name}},
            "NetworkSettings": {"Networks": {self.network.name: {"IPAddress": "172.30.0.3"}}}})
        self.module.ensure_network(self.service, box, "one")
        self.assertEqual(self.module.network_ip(box), "172.30.0.3")
        box.attrs["NetworkSettings"]["Networks"]["other-user-network"] = {}
        with self.assertRaisesRegex(RuntimeError, "unexpected network"):
            self.module.ensure_network(self.service, box, "one")
        self.network.attrs["Labels"]["hugagent.project"] = "another"
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            self.module.remove_network(self.service, "one")

    def test_failed_container_is_removed_but_foreign_members_are_refused(self):
        from unittest.mock import Mock
        failed = SimpleNamespace(id="failed", attrs={"Config": {"Labels": {"opensandbox.io/id": "one"}}},
                                 remove=Mock())
        self.boxes["failed"] = failed
        self.network.attrs["Containers"] = {"failed": {}}
        self.module.remove_network(self.service, "one")
        failed.remove.assert_called_once_with(force=True)
        foreign = SimpleNamespace(id="foreign", attrs={"Config": {"Labels": {}}})
        self.boxes["foreign"] = foreign
        self.network.attrs["Containers"] = {"foreign": {}}
        with self.assertRaisesRegex(RuntimeError, "unexpected sandbox network member"):
            self.module.remove_network(self.service, "one")

    def test_network_lifecycle_when_backend_is_stopped(self):
        self.service.docker_client.containers.list = lambda **kw: []
        self.module.create_network(self.service, "one")
        self.assertEqual(set(self.network.attrs["Containers"]), {"controller"})
        self.module.remove_network(self.service, "one")
        self.network.remove.assert_called_once()

    def test_lists_only_the_current_compose_projects_sandboxes(self):
        own = SimpleNamespace(attrs={"Config": {"Labels": {
            "opensandbox.io/id": "one", "hugagent.sandbox-network": "test-sandbox-one"}}})
        foreign = SimpleNamespace(attrs={"Config": {"Labels": {
            "opensandbox.io/id": "two", "hugagent.sandbox-network": "foreign-sandbox-two"}}})
        self.service.docker_client.containers.list = lambda **kw: [own, foreign]
        self.assertEqual(self.module.owned_sandboxes(self.service), [own])

    def test_connection_failure_rolls_back_owned_network(self):
        self.network.connect.side_effect = RuntimeError("attach failed")
        with self.assertRaisesRegex(RuntimeError, "attach failed"):
            self.module.create_network(self.service, "one")
        self.network.remove.assert_called_once()


if __name__ == "__main__":
    unittest.main()

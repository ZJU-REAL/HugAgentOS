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
    else Path(__file__).resolve().parents[4] / "docker/opensandbox-server/patch_opensandbox_server.py"
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
    async def test_creation_does_not_block_controller_queries(self):
        import asyncio
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        def slow_create(request):
            time.sleep(0.25)
            return "created"
        service = SimpleNamespace(_create_sandbox_sync=slow_create)
        fn = extract(source, "create_sandbox", {"run_in_threadpool": run_in_threadpool})
        started = time.perf_counter()
        creation = asyncio.create_task(fn(service, object()))
        await asyncio.sleep(0.02)
        self.assertLess(time.perf_counter() - started, 0.15)
        self.assertEqual(await creation, "created")

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
            },
        )
        seen = []
        box = SimpleNamespace(attrs={"Config": {"Labels": {"opensandbox.io/id": "test"}},
                                    "NetworkSettings": {"Networks": {"test-shared": {}}}})

        def get(name):
            seen.append(name)
            return box

        service = SimpleNamespace(
            docker_client=SimpleNamespace(containers=SimpleNamespace(get=get)),
            network_mode="test-shared",
            _owns_sandbox=lambda box: "test-shared" in box.attrs.get("NetworkSettings", {}).get("Networks", {}),
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

    def test_shared_network_provisioning_and_endpoints_match_upstream(self):
        sources = patch.patched_sources(SOURCE)
        self.assertNotIn("services/docker/networking.py", sources)
        controller = sources["services/docker/docker_service.py"]
        upstream = (SOURCE / "services/docker/docker_service.py").read_text()
        start = "            if request.network_policy:"
        end = "            # Inject volume bind mounts"
        self.assertEqual(controller[controller.index(start):controller.index(end)],
                         upstream[upstream.index(start):upstream.index(end)])
        self.assertNotIn("isolated_networks", controller)
        self.assertNotIn("create_network(", controller)
        self.assertNotIn("ensure_network(", controller)

    def test_discovery_is_scoped_to_configured_shared_network(self):
        from unittest.mock import Mock
        service = SimpleNamespace(network_mode="project_shared", _owns_sandbox=lambda box: True,
            docker_client=SimpleNamespace(containers=SimpleNamespace(list=Mock(return_value=[]))))
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        fn = extract(source, "_owned_sandboxes", {"SANDBOX_ID_LABEL": "opensandbox.io/id"})
        self.assertEqual(fn(service), [])
        service.docker_client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["opensandbox.io/id"]})

    def test_lookup_rejects_another_controllers_network(self):
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        box = SimpleNamespace(attrs={"Config": {"Labels": {"opensandbox.io/id": "test"}},
            "NetworkSettings": {"Networks": {"another-project": {}}}})
        service = SimpleNamespace(network_mode="project_shared", _owns_sandbox=lambda box: False,
            docker_client=SimpleNamespace(containers=SimpleNamespace(get=lambda _: box)))
        fn = extract(source, "_get_container_by_sandbox_id", {
            "NotFound": KeyError, "DockerException": RuntimeError, "HTTPException": HTTPException,
            "status": SimpleNamespace(HTTP_404_NOT_FOUND=404),
            "SandboxErrorCodes": SimpleNamespace(SANDBOX_NOT_FOUND="missing"),
            "SANDBOX_ID_LABEL": "opensandbox.io/id"})
        with self.assertRaises(HTTPException) as failure:
            fn(service, "test")
        self.assertEqual(failure.exception.status_code, 404)

    def test_sidecar_network_namespace_keeps_existing_policy_sandboxes_owned(self):
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        sidecar = SimpleNamespace(attrs={"Config": {"Labels": {"policy-owner": "sandbox"}},
            "NetworkSettings": {"Networks": {"bridge": {}}}})
        box = SimpleNamespace(attrs={"Config": {"Labels": {"sandbox-id": "sandbox"}},
            "HostConfig": {"NetworkMode": "container:sidecar"},
            "NetworkSettings": {"Networks": {}}})
        service = SimpleNamespace(network_mode="bridge", docker_client=SimpleNamespace(
            containers=SimpleNamespace(get=lambda name: sidecar)))
        fn = extract(source, "_owns_sandbox", {"NotFound": KeyError,
            "SANDBOX_ID_LABEL": "sandbox-id", "EGRESS_SIDECAR_LABEL": "policy-owner"})
        self.assertTrue(fn(service, box))
        sidecar.attrs["Config"]["Labels"]["policy-owner"] = "another-sandbox"
        self.assertFalse(fn(service, box))

    def test_startup_discovers_only_configured_network_policy_sidecars(self):
        from unittest.mock import Mock
        orphan = object()
        service = SimpleNamespace(network_mode="bridge", _owned_sandboxes=lambda: [],
            docker_client=SimpleNamespace(containers=SimpleNamespace(list=Mock(return_value=[orphan]))))
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        fn = extract(source, "_owned_runtime_containers", {"EGRESS_SIDECAR_LABEL": "policy-owner"})
        self.assertEqual(fn(service), [orphan])
        service.docker_client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["policy-owner"], "network": "bridge"})

    def test_startup_cleans_orphan_policy_sidecars(self):
        from datetime import datetime, timezone
        from threading import Lock
        from unittest.mock import Mock
        orphan = SimpleNamespace(attrs={"Config": {"Labels": {"policy-owner": "orphan"}}})
        service = SimpleNamespace(_owned_runtime_containers=lambda: [orphan],
            _ossfs_mount_lock=Lock(),
            _get_container_by_sandbox_id=Mock(side_effect=HTTPException(404, "missing")),
            _cleanup_egress_sidecar=Mock())
        source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        fn = extract(source, "_restore_existing_sandboxes", {
            "DockerException": RuntimeError, "logger": Mock(),
            "datetime": datetime, "timezone": timezone,
            "SANDBOX_ID_LABEL": "sandbox-id", "EGRESS_SIDECAR_LABEL": "policy-owner",
            "HTTPException": HTTPException, "status": SimpleNamespace(HTTP_404_NOT_FOUND=404)})
        fn(service)
        service._cleanup_egress_sidecar.assert_called_once_with("orphan")

    def test_controller_enforces_no_capacity_and_creates_like_upstream(self):
        # Capacity belongs to the backend admission ledger; a controller-side
        # lock around creation would serialize every session's sandbox start.
        controller = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
        upstream = (SOURCE / "services/docker/docker_service.py").read_text()
        self.assertNotIn("sandbox_capacity", controller)
        self.assertNotIn("flock", controller)
        call = "            created_container = self._create_and_start_container(\n"
        self.assertIn(call, upstream)
        self.assertIn(call, controller)
        self.assertIn("install_inventory()", controller)

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



def _inventory_module():
    candidates = (PATCH_PATH.parent / "opensandbox_inventory.py",
                  SOURCE / "services/docker/inventory.py")
    path = next(candidate for candidate in candidates if candidate.is_file())
    spec = importlib.util.spec_from_file_location("controller_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(importlib.util.find_spec("docker"), "docker SDK is a controller dependency")
class InventoryTests(unittest.TestCase):
    def setUp(self):
        from docker.models.containers import ContainerCollection
        self.original = ContainerCollection.list
        self.addCleanup(setattr, ContainerCollection, "list", self.original)

    def _collection(self, removed):
        from docker.errors import NotFound
        from docker.models.containers import ContainerCollection

        class Api:
            def containers(self, **kwargs):
                return [{"Id": "gone"}, {"Id": "live"}]

            def inspect_container(self, container_id):
                if container_id in removed:
                    raise NotFound("No such container: " + container_id)
                return {"Id": container_id}

        return ContainerCollection(client=SimpleNamespace(api=Api()))

    def test_listing_skips_containers_removed_while_enumerating(self):
        from docker.errors import NotFound
        collection = self._collection({"gone"})
        with self.assertRaises(NotFound):
            collection.list(all=True)  # docker-py default: one deletion fails the listing
        _inventory_module().install()
        self.assertEqual([c.id for c in collection.list(all=True, filters={"label": ["x"]})],
                         ["live"])

    def test_install_is_idempotent_and_explicit_flags_win(self):
        from docker.errors import NotFound
        from docker.models.containers import ContainerCollection
        module = _inventory_module()
        module.install()
        installed = ContainerCollection.list
        module.install()
        self.assertIs(ContainerCollection.list, installed)
        with self.assertRaises(NotFound):
            self._collection({"gone"}).list(all=True, ignore_removed=False)


if __name__ == "__main__":
    unittest.main()

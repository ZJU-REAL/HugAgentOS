"""Pinned OpenSandbox integration: configured shared network and nonblocking APIs.

The backend admission ledger decides whether a sandbox may be created; the
controller only performs Docker operations.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path

EXPECTED = {
    "api/lifecycle.py": "6d6f437db5fc2de9645021fc8afdcd980883e1178b2d57737a4c9cb87ae738ba",
    "api/proxy.py": "a0f22b64dd5e9f2a045d4d8e6a66a2b118cd9addb934550224945f48b67e317e",
    "services/docker/docker_service.py": "0cf2ab543c44705a7118e08135f945f732699f74a76946f419d1a91460fbcf9f",
}
LOOKUP = '''    def _owns_sandbox(self, container):
        """Resolve the actual Docker network namespace before checking ownership."""
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        mode = container.attrs.get("HostConfig", {}).get("NetworkMode", "")
        if mode.startswith("container:"):
            sandbox_id = container.attrs.get("Config", {}).get("Labels", {}).get(SANDBOX_ID_LABEL)
            try:
                sidecar = self.docker_client.containers.get(mode.split(":", 1)[1])
            except NotFound:
                return False
            if not sandbox_id or sidecar.attrs.get("Config", {}).get("Labels", {}).get(EGRESS_SIDECAR_LABEL) != sandbox_id:
                return False
            networks = sidecar.attrs.get("NetworkSettings", {}).get("Networks", {})
        return self.network_mode in networks

    def _owned_sandboxes(self):
        """Enumerate only sandboxes using this controller's configured network."""
        containers = self.docker_client.containers.list(
            all=True, filters={"label": [SANDBOX_ID_LABEL]},
        )
        return [container for container in containers if self._owns_sandbox(container)]

    def _owned_runtime_containers(self):
        """Include network-scoped policy sidecars for startup orphan cleanup."""
        sidecars = self.docker_client.containers.list(
            all=True, filters={"label": [EGRESS_SIDECAR_LABEL], "network": self.network_mode},
        )
        return self._owned_sandboxes() + sidecars

    def _get_container_by_sandbox_id(self, sandbox_id: str):
        """Inspect the deterministic name, then verify the sandbox ownership label."""
        try:
            container = self.docker_client.containers.get(f"sandbox-{sandbox_id}")
        except NotFound:
            container = None
        except DockerException as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.CONTAINER_QUERY_FAILED,
                    "message": f"Failed to query sandbox containers: {str(exc)}",
                },
            ) from exc
        if (
            container is None
            or (container.attrs.get("Config", {}).get("Labels") or {}).get(SANDBOX_ID_LABEL)
            != sandbox_id
            or not self._owns_sandbox(container)
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": SandboxErrorCodes.SANDBOX_NOT_FOUND,
                    "message": f"Sandbox {sandbox_id} not found.",
                },
            )
        return container

'''


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"Expected exactly one patch target: {old!r}")
    return source.replace(old, new, 1)


def patched_sources(root: Path) -> dict[str, str]:
    sources = {}
    for name, digest in EXPECTED.items():
        data = (root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(
                f"OpenSandbox v0.1.13 source drift: {name}; review patch before upgrading"
            )
        sources[name] = data.decode()

    name = "services/docker/docker_service.py"
    source = sources[name]
    start = source.index("    def _get_container_by_sandbox_id(")
    end = source.index("    def _schedule_expiration(", start)
    sources[name] = replace_once(
        source[:start] + LOOKUP + source[end:],
        "import inspect\n",
        "import inspect\nfrom docker.errors import NotFound\n",
    )

    # Creation includes synchronous Docker and FUSE calls. Keep them off the
    # controller event loop, including volume validation and network setup.
    name = "services/docker/docker_service.py"
    source = sources[name]
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom starlette.concurrency import run_in_threadpool\n")
    source = replace_once(source,
        "    async def create_sandbox(self, request: CreateSandboxRequest) -> CreateSandboxResponse:\n",
        "    async def create_sandbox(self, request: CreateSandboxRequest) -> CreateSandboxResponse:\n"
        "        return await run_in_threadpool(self._create_sandbox_sync, request)\n\n"
        "    def _create_sandbox_sync(self, request: CreateSandboxRequest) -> CreateSandboxResponse:\n")
    # Every container enumeration in this process tolerates concurrent removal.
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom .inventory import install as install_inventory\ninstall_inventory()\n")
    sources[name] = source

    name = "api/lifecycle.py"
    source = sources[name]
    calls = (
        ("list_sandboxes", "request"),
        ("get_sandbox", "sandbox_id"),
        ("delete_sandbox", "sandbox_id"),
        ("pause_sandbox", "sandbox_id"),
        ("resume_sandbox", "sandbox_id"),
        ("renew_expiration", "sandbox_id, request"),
        ("get_endpoint", "sandbox_id, port, expires=expires"),
    )
    for method, args in calls:
        source = replace_once(
            source,
            f"sandbox_service.{method}({args})",
            f"await run_in_threadpool(sandbox_service.{method}, {args})",
        )
    sources[name] = replace_once(
        source,
        "import asyncio\n",
        "import asyncio\nfrom starlette.concurrency import run_in_threadpool\n",
    )

    name = "api/proxy.py"
    source = sources[name]
    old = "lifecycle.sandbox_service.get_endpoint(sandbox_id, port, resolve_internal=True)"
    if source.count(old) != 2:
        raise ValueError("Expected HTTP and WebSocket endpoint lookup")
    source = source.replace(
        old,
        "await run_in_threadpool(lifecycle.sandbox_service.get_endpoint, sandbox_id, port, resolve_internal=True)",
    )
    sources[name] = replace_once(
        source,
        "import logging\n",
        "import logging\nfrom starlette.concurrency import run_in_threadpool\n",
    )
    patch_shared_lifecycle(sources)
    patch_durable_expirations(sources)
    patch_namespace_mounts(sources)

    name = "services/docker/docker_service.py"
    sources[name] = replace_once(sources[name], "import inspect\n",
        "import inspect\nfrom .object_mounts import install as install_object_mounts\ninstall_object_mounts()\n")
    name = "services/docker/docker_service.py"
    sources[name] = replace_once(sources[name], "import inspect\n",
        "import inspect\nfrom .mount_recovery import EPOCH as MOUNT_EPOCH\n")
    sources[name] = replace_once(sources[name],
        '        entrypoint = container.attrs.get("Config", {}).get("Cmd") or []',
        '        metadata = {**(metadata or {}), "hugagent-mount-epoch": MOUNT_EPOCH}\n'
        '        entrypoint = container.attrs.get("Config", {}).get("Cmd") or []')
    sources["api/lifecycle.py"] += """
\nfrom opensandbox_server.services.docker.mount_recovery import RecoveryRequest, ensure_mounts

@router.post("/sandboxes/{sandbox_id}/space-mounts/ensure")
async def ensure_space_mounts(sandbox_id: str, request: RecoveryRequest):
    return await run_in_threadpool(ensure_mounts, sandbox_service, sandbox_id, request)
"""
    for name, source in sources.items():
        ast.parse(source, filename=name)
    return sources


def patch_namespace_mounts(sources):
    name = "services/docker/docker_service.py"
    source = sources[name]
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom .object_mounts import MANIFEST, mount_manifest\n"
        "from .mount_recovery import initialize_mounts, cleanup_helpers\n")
    source = replace_once(source, "        self._restore_existing_sandboxes()",
        "        self._restore_existing_sandboxes()\n        cleanup_helpers(self)")
    start = source.index("        # Prepare OSSFS mounts first")
    end = source.index("        sidecar_container = None", start)
    source = source[:start] + (
        "        labels[MANIFEST] = json.dumps(mount_manifest(request.volumes))\n"
        "        ossfs_mount_keys = []\n\n") + source[end:]
    source = replace_once(source,
        "self._build_volume_binds(request.volumes, pvc_inspect_cache)",
        "self._build_volume_binds([v for v in (request.volumes or []) if v.ossfs is None], pvc_inspect_cache)")
    source = replace_once(source,
        "            if expires_at is not None:\n                self._schedule_expiration(sandbox_id, expires_at)",
        "            initialize_mounts(self, sandbox_id, request)\n"
        "            if expires_at is not None:\n                self._schedule_expiration(sandbox_id, expires_at)")
    source = replace_once(source,
        "                self._cleanup_windows_oem_volume(sandbox_id, None)",
        "                remove_helper(self, sandbox_id)\n"
        "                self._cleanup_windows_oem_volume(sandbox_id, None)")
    sources[name] = source


def patch_durable_expirations(sources):
    name = "services/docker/docker_service.py"
    source = sources[name]
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom .expirations import ExpirationStore\n")
    source = replace_once(source, "        self._restore_existing_sandboxes()",
        "        self._expiration_store = ExpirationStore()\n"
        "        self._restore_existing_sandboxes()")
    source = replace_once(source,
        "        with self._expiration_lock:\n            # Replace existing timer",
        "        with self._expiration_lock:\n"
        "            if update_expiration:\n"
        "                self._expiration_store.put(sandbox_id, expires_at.isoformat())\n"
        "            # Replace existing timer")
    source = replace_once(source,
        '        """Remove expiration tracking and cancel any pending timers."""\n        with self._expiration_lock:\n',
        '        """Remove expiration tracking and cancel any pending timers."""\n        with self._expiration_lock:\n'
        '            try:\n'
        '                self._expiration_store.delete(sandbox_id)\n'
        '            except Exception:\n'
        '                logger.exception("Failed to remove durable lease for sandbox %s; continuing resource cleanup", sandbox_id)\n')
    source = replace_once(source,
        '            expires_label = labels.get(SANDBOX_EXPIRES_AT_LABEL)',
        '            expires_label = self._expiration_store.get(sandbox_id) or labels.get(SANDBOX_EXPIRES_AT_LABEL)')
    # Lease creation belongs to the same rollback boundary as container creation.
    source = replace_once(source,
        "        sidecar_container = None\n        try:",
        "        sidecar_container = None\n        created_container = None\n        try:")
    source = replace_once(source,
        "                request.platform,\n            )\n        except Exception:",
        "                request.platform,\n            )\n"
        "            if expires_at is not None:\n"
        "                self._schedule_expiration(sandbox_id, expires_at)\n"
        "        except Exception:\n"
        "            if created_container is not None:\n"
        "                try:\n"
        "                    created_container.remove(force=True)\n"
        "                except DockerException:\n"
        "                    logger.exception(\"Failed to rollback sandbox %s after provisioning failure\", sandbox_id)")
    source = replace_once(source,
        "        if expires_at is not None:\n            self._schedule_expiration(sandbox_id, expires_at)\n\n        effective_platform",
        "        effective_platform")
    # Persist leases in the controller data volume. Docker's update endpoint
    # ignores Labels; do not claim that an in-memory renewal changed them.
    start = source.index("        # Persist the new timeout in memory;")
    end = source.index("        return RenewSandboxExpirationResponse", start)
    source = source[:start] + "        self._schedule_expiration(sandbox_id, new_expiration)\n\n" + source[end:]
    sources[name] = source


def patch_shared_lifecycle(sources):
    # Keep project/controller ownership and later mount recovery; restore the
    # upstream configured network and host-port endpoint implementation unchanged.
    name = "services/docker/docker_service.py"
    source = sources[name]
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom .mount_recovery import remove_helper\n")
    start = source.index("    def _restore_existing_sandboxes(")
    end = source.index("    def _container_to_sandbox(", start)
    block = replace_once(source[start:end],
        "self.docker_client.containers.list(all=True)", "self._owned_runtime_containers()")
    source = source[:start] + block + source[end:]
    source = replace_once(source,
        '            containers = self.docker_client.containers.list(\n                all=True,\n                filters={"label": [SANDBOX_ID_LABEL]},\n            )',
        "            containers = self._owned_sandboxes()")
    source = replace_once(source,
        "            self._cleanup_managed_volumes(sandbox_id, auto_created_volumes or [])\n",
        "            remove_helper(self, sandbox_id)\n"
        "            self._cleanup_managed_volumes(sandbox_id, auto_created_volumes or [])\n")
    source = replace_once(source,
        "            self._cleanup_managed_volumes(sandbox_id, managed_volumes)\n",
        "            remove_helper(self, sandbox_id)\n"
        "            self._cleanup_managed_volumes(sandbox_id, managed_volumes)\n")
    start = source.index("    def _expire_sandbox(")
    end = source.index("    def _restore_existing_sandboxes(", start)
    block = replace_once(source[start:end],
        "        self._release_ossfs_mounts(mount_keys)\n",
        "        remove_helper(self, sandbox_id)\n"
        "        self._release_ossfs_mounts(mount_keys)\n")
    sources[name] = source[:start] + block + source[end:]


def apply(root: Path) -> None:
    # Validate every source and syntax before writing any file.
    for name, source in patched_sources(root).items():
        (root / name).write_text(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    apply(parser.parse_args().root)

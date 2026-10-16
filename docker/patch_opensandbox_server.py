"""Pinned OpenSandbox Docker integration: private bridges and nonblocking APIs.

Validate upstream sources before applying changes. Each sandbox has one private
bridge with no host ports; only the Compose controller and backend may join it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path

EXPECTED = {
    "services/docker/networking.py": "4381dae957afc90233b3a52f0759d13d0e0890986213e14e40806f8457b176ee",
    "api/lifecycle.py": "6d6f437db5fc2de9645021fc8afdcd980883e1178b2d57737a4c9cb87ae738ba",
    "api/proxy.py": "a0f22b64dd5e9f2a045d4d8e6a66a2b118cd9addb934550224945f48b67e317e",
    "services/docker/docker_service.py": "0cf2ab543c44705a7118e08135f945f732699f74a76946f419d1a91460fbcf9f",
}
LOOKUP = '''    def _get_container_by_sandbox_id(self, sandbox_id: str):
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
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": SandboxErrorCodes.SANDBOX_NOT_FOUND,
                    "message": f"Sandbox {sandbox_id} not found.",
                },
            )
        ensure_network(self, container, sandbox_id)
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
    patch_isolated_networks(sources)
    for name, source in sources.items():
        ast.parse(source, filename=name)
    return sources


def patch_isolated_networks(sources):
    name = "services/docker/docker_service.py"
    source = sources[name]
    source = replace_once(source, "import inspect\n",
        "import inspect\nfrom .isolated_networks import (NETWORK_LABEL, create_network, ensure_network, remove_network)\n")
    source = replace_once(source, "        sidecar_container = None\n        try:",
        "        sidecar_container = None\n        isolated_network_name = None\n        try:")
    start = source.index("            if request.network_policy:", source.index("    def _provision_sandbox("))
    end = source.index("            # Inject volume bind mounts", start)
    source = source[:start] + """            if request.network_policy:
                raise RuntimeError("networkPolicy is not supported by isolated Docker bridges")
            isolated_network_name = create_network(self, sandbox_id)
            labels[NETWORK_LABEL] = isolated_network_name
            host_config_kwargs = self._base_host_config_kwargs(
                effective_mem_limit, effective_nano_cpus, isolated_network_name,
                gpu_count=effective_gpu_count,
            )
            container_exposed_ports = None

""" + source[end:]
    source = replace_once(source,
        "            self._cleanup_managed_volumes(sandbox_id, auto_created_volumes or [])\n",
        "            if isolated_network_name is not None:\n"
        "                remove_network(self, sandbox_id)\n"
        "            self._cleanup_managed_volumes(sandbox_id, auto_created_volumes or [])\n")
    source = replace_once(source,
        "            self._cleanup_managed_volumes(sandbox_id, managed_volumes)\n",
        "            self._cleanup_managed_volumes(sandbox_id, managed_volumes)\n"
        "        remove_network(self, sandbox_id)\n")
    sources[name] = source

    name = "services/docker/networking.py"
    source = sources[name]
    # Import immediately after future imports, before any endpoint is resolved.
    marker = "from __future__ import annotations\n"
    source = replace_once(source, marker, marker + "from .isolated_networks import network_ip\n")
    start = source.index("        if resolve_internal:", source.index("    def get_endpoint("))
    end = source.index("    def _resolve_host_mapped_endpoint(", start)
    source = source[:start] + """        container = self._get_container_by_sandbox_id(sandbox_id)
        return Endpoint(endpoint=f"{network_ip(container)}:{port}")

""" + source[end:]
    # Proxy and direct SDK endpoints use the same private network identity.
    start = source.index("    def _extract_bridge_ip(")
    source = source[:start] + """    def _extract_bridge_ip(self, container) -> str:
        return network_ip(container)
"""
    sources[name] = source


def apply(root: Path) -> None:
    # Validate every source and syntax before writing any file.
    for name, source in patched_sources(root).items():
        (root / name).write_text(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    apply(parser.parse_args().root)

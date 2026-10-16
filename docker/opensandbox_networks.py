"""Private Docker bridge per sandbox; only controller and backend may join."""
from __future__ import annotations

import os
from threading import RLock

NETWORK_LABEL = "hugagent.sandbox-network"
PROJECT_LABEL = "hugagent.project"
SANDBOX_LABEL = "hugagent.sandbox"
_lock = RLock()


def _trusted(service):
    controller = service.docker_client.containers.get(os.environ["HOSTNAME"])
    labels = controller.attrs["Config"]["Labels"]
    project = labels["com.docker.compose.project"]
    backends = service.docker_client.containers.list(filters={"label": [
        "com.docker.compose.project=" + project,
        "com.docker.compose.service=backend",
    ]})
    # Keep the management DNS alias on the permanent Compose network.
    return project, [(controller, "sandbox-control"), *[(box, "backend") for box in backends]]


def _checked_network(service, sandbox_id):
    project, trusted = _trusted(service)
    network = service.docker_client.networks.get(project + "-sandbox-" + sandbox_id)
    labels = network.attrs["Labels"]
    if labels.get(PROJECT_LABEL) != project or labels.get(SANDBOX_LABEL) != sandbox_id:
        raise RuntimeError("Sandbox network ownership mismatch")
    return network, trusted


def _connect(network, trusted):
    network.reload()
    attached = network.attrs.get("Containers") or {}
    for box, alias in trusted:
        if box.id not in attached:
            network.connect(box, aliases=[alias])


def _remove(service, network):
    network.reload()
    project = network.attrs["Labels"][PROJECT_LABEL]
    for container_id in list(network.attrs.get("Containers") or {}):
        box = service.docker_client.containers.get(container_id)
        labels = box.attrs["Config"]["Labels"]
        if labels.get("opensandbox.io/id") == network.attrs["Labels"][SANDBOX_LABEL]:
            box.remove(force=True)
            continue
        if (labels.get("com.docker.compose.project") != project or
                labels.get("com.docker.compose.service") not in ("backend", "opensandbox")):
            raise RuntimeError("Refusing to detach an unexpected sandbox network member")
        network.disconnect(box)
    network.remove()


def create_network(service, sandbox_id):
    with _lock:
        project, trusted = _trusted(service)
        name = project + "-sandbox-" + sandbox_id
        network = service.docker_client.networks.create(
            name, driver="bridge",
            labels={PROJECT_LABEL: project, SANDBOX_LABEL: sandbox_id},
        )
        try:
            _connect(network, trusted)
        except Exception:
            _remove(service, network)
            raise
        return name


def ensure_network(service, container, sandbox_id):
    with _lock:
        network, trusted = _checked_network(service, sandbox_id)
        if container.attrs["Config"]["Labels"].get(NETWORK_LABEL) != network.name:
            raise RuntimeError("Sandbox does not belong to its isolated network")
        if set(container.attrs["NetworkSettings"]["Networks"]) != {network.name}:
            raise RuntimeError("Sandbox has an unexpected network attachment")
        _connect(network, trusted)


def remove_network(service, sandbox_id):
    with _lock:
        network, _ = _checked_network(service, sandbox_id)
        _remove(service, network)


def network_ip(container):
    name = container.attrs["Config"]["Labels"][NETWORK_LABEL]
    ip = container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"]
    if not ip:
        raise RuntimeError("Isolated sandbox has no network address")
    return ip


def owned_sandboxes(service):
    project, _ = _trusted(service)
    result = []
    for box in service.docker_client.containers.list(all=True, filters={"label": NETWORK_LABEL}):
        labels = box.attrs["Config"]["Labels"]
        sandbox_id = labels.get("opensandbox.io/id")
        if sandbox_id and labels.get(NETWORK_LABEL) == project + "-sandbox-" + sandbox_id:
            result.append(box)
    return result

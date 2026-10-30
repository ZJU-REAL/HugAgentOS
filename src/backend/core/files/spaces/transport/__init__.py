"""Select the space transport for this deployment.

Docker deployments follow ``STORAGE_TYPE``. Kubernetes deployments follow
``HUGAGENT_SPACE_MOUNT_MODE``: ``csi`` (default), ``sidecar`` or ``sync``.
Configuration is read on every call so a changed environment cannot leave a
stale transport behind; instances are stateless.
"""
import os

from core.files.spaces.transport.base import SpaceTransport, SyncReport

KUBERNETES_MODES = ("csi", "sidecar", "sync")


def kubernetes_mode() -> str:
    mode = os.getenv("HUGAGENT_SPACE_MOUNT_MODE", "csi").strip().lower() or "csi"
    if mode not in KUBERNETES_MODES:
        raise RuntimeError(
            f"HUGAGENT_SPACE_MOUNT_MODE={mode!r} is not supported; use one of {', '.join(KUBERNETES_MODES)}")
    return mode


def get_space_transport() -> SpaceTransport:
    if os.getenv("DEPLOY_PROFILE") == "kubernetes":
        mode = kubernetes_mode()
        if mode == "sidecar":
            from core.files.spaces.transport.k8s_sidecar import KubernetesSidecarTransport
            return KubernetesSidecarTransport()
        if mode == "sync":
            from core.files.spaces.transport.sync import SyncTransport
            return SyncTransport()
        from core.files.spaces.transport.k8s_csi import KubernetesCsiTransport
        return KubernetesCsiTransport()
    if os.getenv("STORAGE_TYPE", "local").lower() == "local":
        from core.files.spaces.transport.host import HostTransport
        return HostTransport()
    from core.files.spaces.transport.object_fs import ObjectFsTransport
    return ObjectFsTransport()


def validate_space_transport() -> str:
    """Fail at startup on a configuration the selected transport cannot serve."""
    transport = get_space_transport()
    transport.validate()
    return transport.name


__all__ = ["SpaceTransport", "SyncReport", "get_space_transport", "validate_space_transport"]

"""Docker deployments with local storage bind the host directory directly."""
from pathlib import Path

from fastapi import HTTPException

from core.files.spaces.transport.base import SpaceTransport


class HostTransport(SpaceTransport):
    name = "host"

    def volume(self, grant):
        from opensandbox.models.sandboxes import Host
        from core.config.settings import settings

        root = settings.storage.root.resolve()
        path = root / grant.prefix
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise HTTPException(503, "Invalid local space root")
        host_root = settings.sandbox.opensandbox_host_storage_path
        if not host_root:
            raise HTTPException(503, "HOST_STORAGE_PATH is required for space volumes")
        return self._volume(grant, host=Host(path=str(Path(host_root) / grant.prefix)))

"""Shared preparation of a grant prefix for Kubernetes mount transports."""
import os

from core.files.spaces.transport.base import SpaceTransport
from core.files.storage.entries import validate_key


class KubernetesMountTransport(SpaceTransport):
    def validate(self):
        if os.getenv("STORAGE_TYPE", "").lower() != "s3":
            raise RuntimeError("Kubernetes space transports require STORAGE_TYPE=s3")

    @staticmethod
    def _prepared_prefix(grant) -> str:
        """The grant prefix as a subPath; the trusted backend creates its directory
        marker, since Kubernetes subPath needs a real directory even for a
        read-only member's first access."""
        from core.files.storage import get_storage
        prefix = grant.prefix.rstrip("/")
        validate_key(prefix)
        get_storage().make_directory(grant.prefix)
        return prefix

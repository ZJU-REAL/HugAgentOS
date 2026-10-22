"""Persistent sandbox provider for personal accounts."""
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.files.spaces.sandbox import SpaceSandboxMixin


class PersistentSandboxProvider(SpaceSandboxMixin, OpenSandboxProvider):
    pass

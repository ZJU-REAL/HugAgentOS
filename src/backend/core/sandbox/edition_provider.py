"""Persistent sandbox provider for personal accounts."""
from core.sandbox.opensandbox_provider import OpenSandboxProvider
from core.spaces.sandbox import SpaceSandboxMixin


class PersistentSandboxProvider(SpaceSandboxMixin, OpenSandboxProvider):
    pass

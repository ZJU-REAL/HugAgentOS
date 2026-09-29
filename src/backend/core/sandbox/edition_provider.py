"""Persistent sandboxes are not shipped in Community Edition."""


def PersistentSandboxProvider():
    raise ModuleNotFoundError("Persistent sandbox is unavailable in this edition")

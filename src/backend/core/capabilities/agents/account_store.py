"""account store responsibilities."""
from __future__ import annotations


def desktop_store() -> bool:
    """Whether this process keeps a desktop capability file store (agents projected there)."""
    from core.harness.registry.run_capabilities import has_run_capabilities

    return has_run_capabilities()

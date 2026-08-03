"""GCE Harness — governed co-evolution control system.

This package holds the *shared* contract for the evolution loop: what an
Episode is, what an asset version is, what a candidate change looks like.  The
parts that propose, evaluate and release candidates are enterprise-edition
concerns and live under ``edition_ee/evolution``; what stays here is the
protocol plus no-op hooks, so the community edition can still record local
evidence without carrying the control plane.

Reading order:
- ``contract.py``   — types shared by every layer (AssetRef, AssetBundle, ...)
- ``runtime_binding.py`` — freezes the asset versions a single run uses
"""

from core.evolution.contract import (
    ASSET_KINDS,
    AssetBundle,
    AssetKind,
    AssetRef,
)
from core.evolution.runtime_binding import (
    bind_runtime_assets,
    get_bundle,
    resolve_bundle_for_run,
)

__all__ = [
    "ASSET_KINDS",
    "AssetBundle",
    "AssetKind",
    "AssetRef",
    "bind_runtime_assets",
    "get_bundle",
    "resolve_bundle_for_run",
]

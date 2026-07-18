"""FastAPI dependency: license feature-flag guard for EE routes (second line of defense).

The first line of defense is the route registry (the CE tree physically lacks
EE routes); this guard protects against the scenario where "the commercial
edition deployed the full codebase, but the license did not purchase a given
capability pack." Under internal deployment (no license file) everything is
allowed through, consistent with historical behavior.
"""

from __future__ import annotations

from fastapi import Depends

from .features import Feature, FeatureNotLicensed
from .manager import license_manager


def requires_feature(feature: Feature):
    async def _dep() -> None:
        if not license_manager.has(feature):
            # The envelope (402 / code 40201) has a single source in
            # FeatureNotLicensed, rendered uniformly by the global error_handler.
            raise FeatureNotLicensed(feature, data={"mode": license_manager.mode()})

    return Depends(_dep)

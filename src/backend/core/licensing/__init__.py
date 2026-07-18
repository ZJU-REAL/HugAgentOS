"""Edition / License facade (CE/EE shared interface layer).

The CE derived tree only carries this package's stub semantics (``has()`` always False);
real signature verification / entitlement belongs to later commercial-edition work and does not appear in this package.
"""

from .deps import requires_feature
from .features import Feature, FeatureNotLicensed, SeatLimitExceeded
from .manager import LicenseManager, license_manager

__all__ = [
    "Feature",
    "FeatureNotLicensed",
    "LicenseManager",
    "SeatLimitExceeded",
    "license_manager",
    "requires_feature",
]

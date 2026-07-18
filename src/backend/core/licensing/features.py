"""EE-exclusive feature-flag enum.

The boundary matches the official pricing page: automation / batch execution /
data canvas (personal) / L2-L3 memory are community-edition capabilities and are
not in this enum — this only lists "organization-level" commercial capabilities.
"""

from enum import Enum
from typing import Optional

from core.infra.exceptions import AppException


class FeatureNotLicensed(AppException):
    """The current license does not authorize this feature flag (both CE/EE trees share the same definition).

    An AppException subclass — mapped by the global error_handler uniformly into
    the 402 envelope {code:40201, message, data:{feature, ...}}; this is the
    **only** source of the license 402 envelope, so route/service layers should
    not hand-roll HTTPException(402) again. 402 rather than 403: a 403 would be
    treated by the frontend as an expired token and force a logout.
    """

    def __init__(self, feature: "Feature", message: Optional[str] = None,
                 data: Optional[dict] = None):
        self.feature = feature
        payload = {"feature": feature.value}
        if data:
            payload.update(data)
        super().__init__(
            code=40201,
            message=message or f"该功能未在当前 license 中授权: {feature.value}",
            status_code=402,
            data=payload,
        )


class SeatLimitExceeded(AppException):
    """Insufficient seats / invalid license prevents adding a user (402, code 40202).

    The service layer raises this exception rather than HTTPException — the HTTP
    semantics are honored uniformly by the error_handler, and non-HTTP callers
    (optional-auth, scripts) can catch it precisely by type.
    """

    def __init__(self, message: str, data: Optional[dict] = None):
        super().__init__(code=40202, message=message, status_code=402, data=data or {})


class Feature(str, Enum):
    SSO = "sso"
    MULTI_TENANCY = "multi_tenancy"
    AUDIT = "audit"
    MEMORY_AUDIT = "memory_audit"
    BILLING = "billing"
    QUOTA = "quota"
    PERSISTENT_SANDBOX = "persistent_sandbox"
    CLOUD_STORAGE = "cloud_storage"
    INDUSTRY_TOOLS = "industry_tools"
    CONTENT_ADMIN = "content_admin"
    SYSTEM_CONFIG = "system_config"
    CANVAS_COLLAB = "canvas_collab"
    WHITELABEL = "whitelabel"
    MODEL_GATEWAY = "model_gateway"

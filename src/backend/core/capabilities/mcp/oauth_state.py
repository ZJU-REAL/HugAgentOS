"""oauth state responsibilities."""
from __future__ import annotations


FLOW_TTL_SECONDS = 10 * 60


DISCOVERY_GRACE_SECONDS = 5.0


all__ = [
    "OAuthBundleStorage",
    "build_oauth_provider",
    "cancel_flow",
    "complete_callback",
    "get_flow_status",
    "public_oauth_callback_url",
    "start_oauth_install",
]

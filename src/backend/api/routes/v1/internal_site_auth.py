"""Shared authorization for internal site callbacks."""

import os
from typing import Optional

from fastapi import HTTPException

def _check_internal_token(token: Optional[str]) -> None:
    expected = os.environ.get("BACKEND_INTERNAL_TOKEN", "")
    if not expected:
        # Same as internal_batch: reject outright when the token is unconfigured in production
        # (fail-closed); only non-production (dev) is let through for local integration testing.
        from core.config.settings import settings

        if settings.server.is_prod:
            raise HTTPException(
                status_code=503,
                detail="internal endpoint disabled: BACKEND_INTERNAL_TOKEN not configured",
            )
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")

"""Which tenant a subject belongs to.

Scattered call sites were each deciding this for themselves, and the usual
decision was the string ``"default"`` written inline. That is *correct* for a
single-tenant deployment and it is not a resolution — it is the absence of one,
which is why a tenant-scoped lookup could ship with no caller passing a tenant
and nothing looking wrong.

One resolver, so scope decisions are made in one place and are auditable. It
reads the user's declared tenant from their shadow record's metadata and falls
back to the deployment tenant. It never guesses from anything else: inferring a
tenant from an email domain or a workspace name is how a scope boundary
silently moves.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"

# Where a subject's tenant is recorded. Metadata rather than a column because
# the deployment model is one tenant per install today; a column would imply a
# multi-tenant story the product does not yet have, and the honest shape is a
# field that is usually absent.
TENANT_METADATA_KEY = "tenant_id"


def tenant_of(user_id: Optional[str]) -> str:
    """This subject's tenant, or the deployment default.

    Degrades to the default on any failure. A scope resolver that raises would
    take down the request path it is called from, and the default is the
    narrowest safe answer in a deployment that has not declared tenants.
    """
    if not user_id:
        return DEFAULT_TENANT
    try:
        from core.db.engine import SessionLocal
        from core.db.models import UserShadow

        with SessionLocal() as db:
            row = db.get(UserShadow, str(user_id))
            if row is None:
                return DEFAULT_TENANT
            metadata = row.extra_data or {}
            if not isinstance(metadata, dict):
                return DEFAULT_TENANT
            declared = str(metadata.get(TENANT_METADATA_KEY) or "").strip()
            return declared or DEFAULT_TENANT
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tenancy] resolution failed for %s: %s", user_id, exc)
        return DEFAULT_TENANT

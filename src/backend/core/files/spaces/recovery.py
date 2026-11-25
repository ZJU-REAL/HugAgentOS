"""Verify an existing sandbox still exposes the account's current grants.

The checks themselves belong to each transport (``core.files.spaces.transport``).
"""


async def ensure_mounts(sandbox, actor, info, grants, *, domain, api_key):
    """``grants`` come from ``authorized_grants`` and match the revision ``info`` carries."""
    from core.files.spaces.transport import get_space_transport
    await get_space_transport().ensure(sandbox, actor, info, grants, domain=domain, api_key=api_key)

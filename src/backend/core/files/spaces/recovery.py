"""Verify an existing sandbox still exposes the account's current grants.

The checks themselves belong to each transport (``core.files.spaces.transport``).
"""


async def ensure_mounts(sandbox, actor, info, *, domain, api_key):
    from core.files.spaces.transport import get_space_transport
    await get_space_transport().ensure(sandbox, actor, info, domain=domain, api_key=api_key)

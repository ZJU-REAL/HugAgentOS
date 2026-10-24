"""A pending permission is answered only by its declared, current server-side authority."""

import asyncio
from core.llm.runtime import interaction_store
from core.sandbox.permissions.account import read_account_mode


async def approve_current_policy(chat_id: str, confirm_id: str, info: dict) -> bool:
    user_id = info.get("_approval_user_id")
    if not user_id or info.get("kind") == "design_pick":
        return False
    if await asyncio.to_thread(read_account_mode, user_id, chat_id) != "full":
        return False
    return await interaction_store.decide(
        "confirm", chat_id, confirm_id, {"decision": "allow", "source": "policy"}
    )

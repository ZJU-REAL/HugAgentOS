"""Loop cancellation must query the current user's active run."""

from types import SimpleNamespace

import pytest
from api.routes.v1 import loops
from tests.db_support import share_session


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_cancel_loop_passes_authenticated_user_to_run_lookup(monkeypatch, active):
    observed = []
    loop = SimpleNamespace(chat_id="chat", status="running")
    monkeypatch.setattr(
        loops,
        "LoopService",
        lambda db: SimpleNamespace(get_loop=lambda loop_id, user_id: loop),
    )

    def get_active(chat_id, user_id):
        observed.append(("lookup", chat_id, user_id))
        return SimpleNamespace(run_id="run") if active else None

    async def cancel(run_id, *, user_id):
        observed.append(("cancel", run_id, user_id))
        return True

    monkeypatch.setattr(loops.runtime, "get_active_run_for_chat", get_active)
    monkeypatch.setattr(loops.runtime, "cancel_run", cancel)
    db = SimpleNamespace(commit=lambda: observed.append(("commit",)))
    share_session(monkeypatch, db)
    result = await loops.cancel_loop("loop", user=SimpleNamespace(user_id="owner"))
    assert result["data"]["cancelled"] is True
    assert observed == (
        [("lookup", "chat", "owner"), ("cancel", "run", "owner")]
        if active
        else [("lookup", "chat", "owner"), ("commit",)]
    )
    if not active:
        assert loop.status == "cancelled"

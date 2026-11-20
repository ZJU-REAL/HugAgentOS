from types import SimpleNamespace
from unittest.mock import AsyncMock

from personal_agent.computer.sandbox import PersonalSandboxProvider


async def test_unbound_computer_clients_are_closed_and_forgotten():
    provider = PersonalSandboxProvider.__new__(PersonalSandboxProvider)
    provider._init_session_locks()
    gone, kept = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    provider._sessions = {"pa_box_gone": SimpleNamespace(sandbox=gone), "pa_box_kept": SimpleNamespace(sandbox=kept)}
    provider._alive_at = {"pa_box_gone": 1.0, "pa_box_kept": 1.0}

    assert await provider.forget_unbound_sessions(iter(["pa_box_kept"])) == 1

    gone.close.assert_awaited_once()
    kept.close.assert_not_awaited()
    assert list(provider._sessions) == ["pa_box_kept"]
    assert list(provider._alive_at) == ["pa_box_kept"]


async def test_client_cached_after_the_bindings_read_is_left_alone(monkeypatch):
    import personal_agent.computer.sandbox as module

    provider = PersonalSandboxProvider.__new__(PersonalSandboxProvider)
    provider._init_session_locks()
    old, new = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    provider._sessions = {"pa_box_old": SimpleNamespace(sandbox=old)}
    provider._alive_at = {}
    monkeypatch.setattr(module, "_provider", provider)

    candidates = module.cached_computer_ids()
    # A computer bound and first used between the snapshot and the bindings read.
    provider._sessions["pa_box_new"] = SimpleNamespace(sandbox=new)

    assert await module.forget_unbound_computers(iter([]), candidates) == 1
    old.close.assert_awaited_once()
    new.close.assert_not_awaited()
    assert list(provider._sessions) == ["pa_box_new"]


async def test_sweep_is_a_noop_before_the_provider_exists(monkeypatch):
    import personal_agent.computer.sandbox as module

    monkeypatch.setattr(module, "_provider", None)
    assert module.cached_computer_ids() == set()
    assert await module.forget_unbound_computers(iter([]), set()) == 0

"""A paused lease holder must not publish or delete after Redis changes owner."""
import asyncio
import json
import time

import pytest
import core.sandbox.sessions.registry as session_registry


@pytest.mark.parametrize("operation", ["publish", "forget"])
def test_expired_claim_cannot_mutate_new_owners_binding(operation, sandbox_session_store):
    async def scenario():
        async with session_registry.claim("chat"):
            await session_registry.remember("chat", "same")
            key = session_registry._LOCK_KEY.format(session_id="chat")
            # Model process suspension past TTL before its heartbeat can run.
            await sandbox_session_store.drop(key)
            assert await sandbox_session_store.hold(key, "new-owner", ttl=300)
            new_id = "new-container" if operation == "publish" else "same"
            values = {
                session_registry._KEY.format(session_id="chat"): json.dumps({"sandbox_id": new_id, "active_at": time.time()}),
                session_registry._REVERSE_KEY.format(sandbox_id=new_id): "chat",
            }
            assert await sandbox_session_store.mutate_if({}, values, ttl=300)
            with pytest.raises(RuntimeError, match="lease lost"):
                if operation == "publish":
                    await session_registry.remember("chat", "obsolete")
                else:
                    await session_registry.forget("chat", "same")
            assert await session_registry.lookup("chat") == new_id
            assert await session_registry.bound_session(new_id) == "chat"
            assert not session_registry.claim_valid()
    asyncio.run(scenario())


def test_background_claim_does_not_inherit_a_released_request_lease():
    async def scenario():
        resume = asyncio.Event()
        async def background():
            await resume.wait()
            async with session_registry.claim("background"):
                await session_registry.remember("background", "created")
        async with session_registry.claim("request"):
            child = asyncio.create_task(background())
        resume.set()
        await child
        assert await session_registry.lookup("background") == "created"
    asyncio.run(scenario())


def test_same_task_nested_claim_still_fences_a_lost_parent(sandbox_session_store):
    async def scenario():
        async with session_registry.claim("request"):
            async with session_registry.claim("creation"):
                key = session_registry._LOCK_KEY.format(session_id="request")
                await sandbox_session_store.drop(key)
                assert await sandbox_session_store.hold(key, "replacement", ttl=300)
                with pytest.raises(RuntimeError, match="lease lost"):
                    await session_registry.remember("request", "obsolete")
                assert await session_registry.lookup("request") is None
                assert not session_registry.claim_valid()
                from core.sandbox.providers.opensandbox.accounting import finish_accounting
                async def accounting_child():
                    # Shielded cleanup still needs to observe its owner's loss.
                    assert not session_registry.claim_valid()
                await finish_accounting(accounting_child())
    asyncio.run(scenario())

"""The memory singleton must not outlive its captured desktop login identity."""

import sys
from types import SimpleNamespace

import httpx
import pytest

from core.capabilities.errors import CloudUnavailable
from core.capabilities.ref import profile_id
from core.memory import service
from core.services import desktop_cloud_bridge as bridge
from tests.capabilities.test_dynamic_model_epoch_cache import state


@pytest.fixture
def memories(monkeypatch, caps_root):
    current = {"state": state(), "instances": [], "on_build": None}
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "synthetic")
    monkeypatch.setattr(bridge, "get_state", lambda: current["state"])
    monkeypatch.setattr(
        service,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(enabled=True, graph_enabled=False)),
    )
    monkeypatch.setattr(service, "_memory_instance", None)
    monkeypatch.setattr(service, "_memory_epoch", None, raising=False)
    monkeypatch.setattr(service, "_memory_init_failed", False)
    monkeypatch.setattr(service, "_patch_mem0_embedding", lambda: None)
    monkeypatch.setattr(service, "_apply_probed_embed_dims", lambda cfg: None)
    monkeypatch.setattr(service, "_reconcile_vector_collection", lambda cfg: None)

    def config():
        value = current["state"] or state()
        from core.services.desktop_capability_protocol import token_subject

        entry = {
            "api_key": "desktop-capability:"
            + profile_id(value["cloud_base"], token_subject(value["token"])),
            "openai_base_url": value["cloud_base"] + "/api/v1/desktop/capability/gateway/models/p1",
        }
        return {"llm": {"config": dict(entry)}, "embedder": {"config": dict(entry)}}

    monkeypatch.setattr(service, "_build_mem0_config", config)

    def build(cfg):
        if current["on_build"]:
            current["on_build"]()
        instance = SimpleNamespace(
            llm=SimpleNamespace(client=SimpleNamespace(close=lambda: None)),
            embedding_model=SimpleNamespace(client=SimpleNamespace(close=lambda: None)),
        )
        current["instances"].append(instance)
        return instance

    monkeypatch.setitem(
        sys.modules, "mem0", SimpleNamespace(Memory=SimpleNamespace(from_config=build))
    )
    yield current
    for instance in current["instances"]:
        instance.llm.client.close()
        instance.embedding_model.client.close()


def authorize(memory):
    client = memory.llm.client._client
    request = httpx.Request("POST", str(memory.llm.client.base_url) + "chat/completions")
    for hook in client.event_hooks["request"]:
        hook(request)
    return request


@pytest.mark.parametrize("replacement", [state(epoch=2, session="new"), state("bob")])
def test_new_epoch_rebuilds_memory_but_keeps_old_instance_guarded(memories, replacement):
    original = service._get_memory()
    assert original is not None
    memories["state"] = replacement
    fresh = service._get_memory()
    assert fresh is not None and fresh is not original
    assert authorize(fresh).headers["Authorization"] == "Bearer " + replacement["token"]
    with pytest.raises(CloudUnavailable):
        authorize(original)


def test_token_refresh_reuses_memory_with_new_header(memories):
    original = service._get_memory()
    memories["state"] = state(nonce="refreshed")
    assert service._get_memory() is original
    assert authorize(original).headers["Authorization"] == "Bearer " + memories["state"]["token"]


def test_logout_never_reuses_old_memory(memories):
    original = service._get_memory()
    memories["state"] = None
    assert service._get_memory() is None
    with pytest.raises(CloudUnavailable):
        authorize(original)


def test_identity_change_during_initialization_is_not_published(memories):
    memories["on_build"] = lambda: memories.update(state=state(epoch=2, session="new"))
    assert service._get_memory() is None
    assert service._memory_instance is None


def test_non_desktop_singleton_does_not_consult_bridge(memories, monkeypatch):
    monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET")
    cached = object()
    monkeypatch.setattr(service, "_memory_instance", cached)
    monkeypatch.setattr(service, "_memory_epoch", "")

    def forbidden():
        raise AssertionError("non-desktop memory must not consult bridge")

    monkeypatch.setattr(bridge, "get_state", forbidden)
    assert service._get_memory() is cached


def test_explicit_runtime_reset_invalidates_instance_and_epoch(memories):
    original = service._get_memory()
    service.reset_runtime()
    assert service._memory_instance is None
    assert service._memory_epoch is None
    assert service._get_memory() is not original

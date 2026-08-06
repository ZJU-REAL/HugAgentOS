from types import SimpleNamespace

from orchestration import workflow


def test_resolve_agent_model_runtime_prefers_baked_model_over_none_fallback():
    agent = SimpleNamespace(
        model=SimpleNamespace(model="deepseekv4-flash", context_size=393_216),
        state=SimpleNamespace(model_name=""),
    )

    assert workflow._resolve_agent_model_runtime(agent, None) == (
        "deepseekv4-flash",
        393_216,
    )


def test_resolve_agent_model_runtime_supports_wrapper_name_and_window_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(
        workflow,
        "resolve_model_context_window",
        lambda model_name: seen.append(model_name) or 131_072,
    )
    agent = SimpleNamespace(
        model=SimpleNamespace(model_name="qwen-max", context_size=0),
        state=SimpleNamespace(model_name=""),
    )

    assert workflow._resolve_agent_model_runtime(agent, "None") == (
        "qwen-max",
        131_072,
    )
    assert seen == ["qwen-max"]

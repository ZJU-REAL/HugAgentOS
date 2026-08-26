"""Context-window auto-discovery: what each source is trusted for, and what it must refuse.

The response bodies below are copied from real upstreams (self-hosted vLLM, a LiteLLM
gateway in front of DashScope, an OpenAI-compatible relay) so the parsers stay pinned to
shapes that actually occur rather than to invented ones.
"""

import httpx
import pytest

from core.llm.providers import context_probe
from core.llm.providers.context_probe import discover_context_length, from_name_heuristic

# One self-hosted vLLM deployment: a single entry that publishes the real window.
_VLLM_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "deepseekr1",
            "object": "model",
            "owned_by": "vllm",
            "root": "/mnt/zju2/GLM-5-w4a8",
            "max_model_len": 131072,
            "permission": [{"id": "modelperm-8cd1e3a3b568dc59"}],
        }
    ],
}

# A relay/gateway listing: many models, no window information anywhere.
_GATEWAY_MODELS = {
    "object": "list",
    "data": [
        {"id": "claude-3-5-haiku-20241022", "object": "model", "owned_by": "anthropic"},
        {"id": "qwen3.7-max", "object": "model", "owned_by": "openai"},
        {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
    ],
}


def _mock_httpx(monkeypatch, handler):
    """Route every AsyncClient the probe builds through ``handler``."""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(context_probe.httpx, "AsyncClient", factory)


async def test_reads_vllm_max_model_len(monkeypatch):
    """The common self-hosted case: the window comes straight off /v1/models."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=_VLLM_MODELS)

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://vllm.internal:10025/v1",
        api_key="sk-test",
        model_name="deepseekr1",
    )

    assert result.context_length == 131072
    assert result.source == "models_endpoint"
    assert result.confidence == "high"
    assert seen == ["http://vllm.internal:10025/v1/models"]


async def test_gateway_without_metadata_falls_back_to_name(monkeypatch):
    """A gateway that publishes no window must not block discovery — but the guess is marked low."""
    _mock_httpx(monkeypatch, lambda request: httpx.Response(200, json=_GATEWAY_MODELS))
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://gateway.internal:8010/v1",
        api_key="sk-test",
        model_name="deepseek-chat",
    )

    assert result.context_length == 64_000
    assert result.source == "name_heuristic"
    assert result.confidence == "low"


async def test_never_borrows_an_unrelated_entrys_window(monkeypatch):
    """On a multi-model listing, a model that is not listed yields no metadata hit.

    Falling back to "the first entry" would silently hand one model another model's
    window — the exact class of error this whole feature exists to avoid.
    """
    listing = {
        "object": "list",
        "data": [
            {"id": "small-model", "max_model_len": 8192},
            {"id": "big-model", "max_model_len": 1_000_000},
        ],
    }
    _mock_httpx(monkeypatch, lambda request: httpx.Response(200, json=listing))
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://gateway.internal/v1",
        api_key="",
        model_name="some-unlisted-model",
        allow_name_heuristic=False,
    )

    assert result.context_length == 0
    assert any("未找到" in note for note in result.notes)


async def test_error_probe_reads_the_window_out_of_a_rejection(monkeypatch):
    """vLLM names max_model_len when it rejects an over-sized max_tokens."""

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-max"}]})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "max_tokens=99999999cannot be greater than "
                        "max_model_len=max_total_tokens=262144."
                    ),
                    "type": "BadRequestError",
                }
            },
        )

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://vllm.internal/v1",
        api_key="sk-test",
        model_name="qwen3-max",
        allow_error_probe=True,
    )

    assert result.context_length == 262144
    assert result.source == "max_tokens_probe"
    assert result.confidence == "medium"


async def test_output_cap_is_reported_but_never_adopted(monkeypatch):
    """A max_tokens range is an *output* cap; adopting it would under-report the window.

    DashScope answers `Range of max_tokens should be [1, 131072]` for a model whose
    context is 262144 — storing 131072 would make the model compact at half its window,
    the regression that made context_length mandatory in the first place.
    """

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.7-max"}]})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "litellm.BadRequestError: DashscopeException - <400> "
                        "InternalError.Algo.InvalidParameter: Range of max_tokens "
                        "should be [1, 131072]."
                    )
                }
            },
        )

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://gateway.internal/v1",
        api_key="sk-test",
        model_name="qwen3.7-max",
        allow_error_probe=True,
        allow_name_heuristic=False,
    )

    assert result.context_length == 0
    assert any("输出上限" in note for note in result.notes)


async def test_ollama_uses_its_native_show_endpoint(monkeypatch):
    """Ollama publishes the window under an architecture-prefixed key."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}},
        )

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="ollama",
        base_url="http://localhost:11434/v1",
        api_key="",
        model_name="qwen3:8b",
    )

    assert result.context_length == 40960
    assert result.source == "ollama_show"
    assert calls[0] == "/api/show"


async def test_unreachable_upstream_degrades_instead_of_raising(monkeypatch):
    """Discovery is a convenience on the config path; a dead endpoint must not break saving."""

    def handler(request):
        raise httpx.ConnectError("connection refused")

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://dead.internal/v1",
        api_key="",
        model_name="claude-3-7-sonnet",
    )

    assert result.context_length == 200_000  # name heuristic still applies
    assert result.source == "name_heuristic"
    assert any("探测失败" in note for note in result.notes)


async def test_no_base_url_skips_network_stages(monkeypatch):
    def handler(request):  # pragma: no cover — must never be called
        raise AssertionError("no HTTP call is allowed without a base_url")

    _mock_httpx(monkeypatch, handler)
    result = await discover_context_length(
        provider="anthropic", base_url="", api_key="", model_name="claude-sonnet-4"
    )

    assert result.context_length == 200_000
    assert result.source == "name_heuristic"


@pytest.mark.parametrize(
    "reported",
    [0, -1, 12, "", None, "not-a-number", 999_999_999_999],
)
async def test_implausible_windows_are_rejected(monkeypatch, reported):
    """Garbage in a metadata field must not become the model's window."""
    _mock_httpx(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": [{"id": "mystery-model", "max_model_len": reported}]}
        ),
    )
    result = await discover_context_length(
        provider="openai_compatible",
        base_url="http://vllm.internal/v1",
        api_key="",
        model_name="mystery-model",
        allow_name_heuristic=False,
    )

    assert result.context_length == 0


async def test_save_time_autofill_never_overrides_a_manual_value(monkeypatch):
    """An operator-supplied window wins, and the "unconfirmed" marker is cleared with it."""
    from api.routes.v1.models import _autofill_context_length

    def handler(request):  # pragma: no cover — must never be called
        raise AssertionError("a filled-in context_length must not trigger a probe")

    _mock_httpx(monkeypatch, handler)
    extra = {"context_length": 262144, "context_length_source": "name_heuristic"}
    await _autofill_context_length(
        "openai_compatible", "chat", "http://vllm.internal/v1", "sk", "deepseekr1", extra
    )

    assert extra == {"context_length": 262144}


async def test_save_time_autofill_fills_an_empty_field_and_records_the_source(monkeypatch):
    from api.routes.v1.models import _autofill_context_length

    _mock_httpx(monkeypatch, lambda request: httpx.Response(200, json=_VLLM_MODELS))
    extra: dict = {}
    await _autofill_context_length(
        "openai_compatible", "chat", "http://vllm.internal/v1", "sk", "deepseekr1", extra
    )

    assert extra == {"context_length": 131072, "context_length_source": "models_endpoint"}


async def test_save_time_autofill_skips_non_chat_providers(monkeypatch):
    """Embedding/reranker providers have no chat context window to trim against."""
    from api.routes.v1.models import _autofill_context_length

    def handler(request):  # pragma: no cover — must never be called
        raise AssertionError("embedding providers must not be probed")

    _mock_httpx(monkeypatch, handler)
    extra: dict = {}
    await _autofill_context_length(
        "openai_compatible", "embedding", "http://vllm.internal/v1", "sk", "bge-m3", extra
    )

    assert extra == {}


def test_name_heuristic_matches_the_frontend_table():
    """These pairs must stay in step with utils/contextUsage.ts::CONTEXT_WINDOWS."""
    assert from_name_heuristic("claude-3-7-sonnet")[0] == 200_000
    assert from_name_heuristic("gpt-4o-mini")[0] == 128_000
    assert from_name_heuristic("qwen2.5-7b-instruct")[0] == 32_768
    assert from_name_heuristic("qwen-max-latest")[0] == 128_000
    assert from_name_heuristic("gemini-2.5-pro")[0] == 1_000_000
    assert from_name_heuristic("some-inhouse-sft-v3")[0] == 0

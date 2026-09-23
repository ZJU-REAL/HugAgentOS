"""跨供应商故障转移：哪些错误该换一家、什么时候还能换、换完谁在答。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from core.llm import failover
from core.llm.failover import FailoverChatModel, provider_is_unusable


def _status(code: int, cls: type = openai.APIStatusError) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://upstream/v1/chat/completions")
    return cls("boom", response=httpx.Response(code, request=request), body=None)


class _Candidate:
    """A stand-in endpoint: raises, or streams, or answers in one shot."""

    def __init__(self, provider_id: str, behaviour: Any, *, stream: bool = True):
        self.provider_id = provider_id
        self.model = f"model-{provider_id}"
        self.credential = SimpleNamespace(api_key=None)
        self.parameters = SimpleNamespace()
        self.stream = stream
        self.context_size = 32000
        self.formatter = SimpleNamespace()
        self.wire_protocol = "openai_chat"
        self.structured_reasoning = False
        self._behaviour = behaviour
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        kind, payload = self._behaviour
        if kind == "raise":
            raise payload
        if kind == "value":
            return payload

        async def _gen():
            for item in payload:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return _gen()


def _build(primary: _Candidate, *fallbacks: _Candidate) -> FailoverChatModel:
    specs = [SimpleNamespace(provider_id=c.provider_id) for c in fallbacks]
    by_pid = {c.provider_id: c for c in fallbacks}
    return FailoverChatModel(primary, specs, lambda spec: by_pid[spec.provider_id])


async def _drain(model: FailoverChatModel) -> list:
    result = await model._call_api("m", [])
    if hasattr(result, "__anext__"):
        return [item async for item in result]
    return result


@pytest.fixture(autouse=True)
def _no_cooldown_leak():
    failover.clear_cooldowns()
    yield
    failover.clear_cooldowns()


# ── 错误分类 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        _status(402),  # 余额不足：openai 没有 402 专用类，落到裸 APIStatusError
        _status(401, openai.AuthenticationError),
        _status(403, openai.PermissionDeniedError),
        _status(404, openai.NotFoundError),
        _status(429, openai.RateLimitError),
        _status(500, openai.InternalServerError),
        openai.APIConnectionError(request=httpx.Request("POST", "http://x")),
    ],
)
def test_provider_faults_switch(exc):
    assert provider_is_unusable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [_status(400, openai.BadRequestError), _status(422, openai.UnprocessableEntityError)],
)
def test_request_faults_do_not_switch(exc):
    """请求本身不合法，换谁都一样——不该拿同一个坏请求去轰别的供应商。"""
    assert provider_is_unusable(exc) is False


# ── 切换行为 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switches_to_next_provider_on_402():
    primary = _Candidate("dead", ("raise", _status(402)))
    backup = _Candidate("alive", ("stream", ["hello"]))
    model = _build(primary, backup)

    assert await _drain(model) == ["hello"]
    assert backup.calls == 1
    # 门面要如实反映真正作答的是谁，否则埋点和回放都会记错供应商。
    assert model.provider_id == "alive"
    assert model.model == "model-alive"


@pytest.mark.asyncio
async def test_switches_when_stream_dies_before_first_chunk():
    primary = _Candidate("dead", ("stream", [_status(402)]))
    backup = _Candidate("alive", ("stream", ["hi"]))
    model = _build(primary, backup)

    assert await _drain(model) == ["hi"]


@pytest.mark.asyncio
async def test_does_not_switch_after_first_chunk():
    """已经吐出内容再换模型，用户会看到前言不搭后语的答案。"""
    primary = _Candidate("dead", ("stream", ["首块", _status(500)]))
    backup = _Candidate("alive", ("stream", ["completely different"]))
    model = _build(primary, backup)

    with pytest.raises(openai.APIStatusError):
        await _drain(model)
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_bad_request_is_not_retried_elsewhere():
    primary = _Candidate("p1", ("raise", _status(400, openai.BadRequestError)))
    backup = _Candidate("p2", ("stream", ["never"]))
    model = _build(primary, backup)

    with pytest.raises(openai.BadRequestError):
        await _drain(model)
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_last_candidate_failure_surfaces():
    primary = _Candidate("p1", ("raise", _status(402)))
    backup = _Candidate("p2", ("raise", _status(402)))
    model = _build(primary, backup)

    with pytest.raises(openai.APIStatusError):
        await _drain(model)


@pytest.mark.asyncio
async def test_healthy_primary_never_builds_fallbacks():
    primary = _Candidate("p1", ("stream", ["ok"]))
    backup = _Candidate("p2", ("stream", ["unused"]))
    built: list[str] = []
    specs = [SimpleNamespace(provider_id="p2")]

    def _builder(spec):
        built.append(spec.provider_id)
        return backup

    model = FailoverChatModel(primary, specs, _builder)
    assert await _drain(model) == ["ok"]
    assert built == []


@pytest.mark.asyncio
async def test_failed_provider_is_skipped_while_cooling():
    primary = _Candidate("dead", ("raise", _status(402)))
    backup = _Candidate("alive", ("stream", ["a"]))
    model = _build(primary, backup)
    await _drain(model)
    assert primary.calls == 1

    backup._behaviour = ("stream", ["b"])
    assert await _drain(model) == ["b"]
    # 冷却期内不再拿它撞墙。
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_all_cooling_still_attempts():
    """全员冷却时也必须试，冷却是优化，不能变成拒绝服务的理由。"""
    failover.mark_provider_down("p1")
    failover.mark_provider_down("p2")
    primary = _Candidate("p1", ("stream", ["served"]))
    backup = _Candidate("p2", ("stream", ["other"]))
    model = _build(primary, backup)

    assert await _drain(model) == ["served"]


@pytest.mark.asyncio
async def test_non_stream_result_passes_through():
    primary = _Candidate("p1", ("value", "one-shot"), stream=False)
    backup = _Candidate("p2", ("value", "unused"), stream=False)
    model = _build(primary, backup)

    assert await _drain(model) == "one-shot"


@pytest.mark.asyncio
async def test_fallback_does_not_leak_into_other_runs():
    """模型实例是跨会话共享缓存的：一个会话回退了，别的会话不能跟着被改。

    context_size 决定 AS2 的压缩阈值，structured_reasoning 决定 SSE 怎么解析
    流——这两个被别的会话改掉，症状是毫不相干的对话开始乱压缩、乱渲染。
    """
    import asyncio

    primary = _Candidate("p1", ("raise", _status(402)))
    backup = _Candidate("p2", ("stream", ["x"]))
    backup.context_size = 8000
    backup.structured_reasoning = True
    model = _build(primary, backup)

    assert await asyncio.create_task(_drain(model)) == ["x"]

    # 回退只在那次调用自己的上下文里成立。
    assert model.context_size == 32000
    assert model.provider_id == "p1"
    assert model.structured_reasoning is False


# ── 装配接线 ───────────────────────────────────────────────────────────


def _cfg(pid: str, ctx: int = 32000):
    return SimpleNamespace(provider_id=pid, model_name=f"model-{pid}", context_length=ctx, extra={})


@pytest.mark.asyncio
async def test_with_failover_switches_end_to_end(monkeypatch):
    """真实装配路径：主供应商 402 时整条链要把答案接住，而不是把错误抛给用户。"""
    from core.llm import failover as mod

    primary_cfg = _cfg("deepseek")
    backup = _Candidate("backup", ("stream", ["接住了"]))

    monkeypatch.setattr(
        "core.services.model_config.ModelConfigService.get_instance",
        lambda: SimpleNamespace(resolve_failover_chain=lambda p: [primary_cfg, _cfg("backup")]),
    )
    monkeypatch.setattr(
        "core.llm.chat_models.build_model_for_mode",
        lambda cfg, **kw: backup,
    )

    primary = _Candidate("deepseek", ("raise", _status(402)))
    model = mod.with_failover(primary, primary_cfg, mode="medium")

    assert isinstance(model, FailoverChatModel)
    assert await _drain(model) == ["接住了"]


def test_with_failover_returns_bare_model_when_alone(monkeypatch):
    """只配了一家时不该平白多包一层。"""
    from core.llm import failover as mod

    only = _cfg("solo")
    monkeypatch.setattr(
        "core.services.model_config.ModelConfigService.get_instance",
        lambda: SimpleNamespace(resolve_failover_chain=lambda p: [only]),
    )
    primary = _Candidate("solo", ("stream", ["x"]))
    assert mod.with_failover(primary, only, mode="medium") is primary


def test_with_failover_survives_a_broken_chain_lookup(monkeypatch):
    """取备选链本身失败时，至少要把主模型原样交出去，不能连累正常请求。"""
    from core.llm import failover as mod

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("core.services.model_config.ModelConfigService.get_instance", _boom)
    primary = _Candidate("p1", ("stream", ["x"]))
    assert mod.with_failover(primary, _cfg("p1"), mode="medium") is primary


# ── 候选顺序 ───────────────────────────────────────────────────────────


class _Row:
    """一行 model_providers，只带排序用得到的字段。"""

    def __init__(self, pid: str, weight: int, ctx: int):
        self.provider_id = pid
        self.display_name = pid
        self.base_url = f"http://{pid}/v1"
        self.api_key = "k"
        self.model_name = pid
        self.provider = "openai_compatible"
        self.provider_type = "chat"
        self.is_active = True
        self.weight = weight
        self.extra_config = {"context_length": ctx}


def _chain_from(rows, monkeypatch, db_session):
    from sqlalchemy.orm import sessionmaker
    from core.services import model_config as mc
    from core.db.model_repository import create_provider

    for row in rows:
        create_provider(
            db_session,
            display_name=row.display_name,
            provider_type="chat",
            base_url=row.base_url,
            api_key=row.api_key,
            model_name=row.model_name,
            weight=row.weight,
            extra_config=row.extra_config,
        )
    monkeypatch.setattr(mc, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    return [c.model_name for c in mc.ModelConfigService().resolve_failover_chain(None)]


def test_chain_prefers_higher_gateway_weight(monkeypatch, db_session):
    """「网关权重」是配置台上唯一能改这个顺序的旋钮，值越大越先用。"""
    rows = [_Row("low", 1, 128000), _Row("high", 9, 32000), _Row("mid", 5, 32000)]
    assert _chain_from(rows, monkeypatch, db_session) == ["high", "mid", "low"]


def test_chain_breaks_weight_ties_on_context_window(monkeypatch, db_session):
    """没人调过权重时（全是默认 1），大窗口优先——切过去才接得住长对话。"""
    rows = [_Row("small", 1, 32000), _Row("big", 1, 1000000), _Row("mid", 1, 128000)]
    assert _chain_from(rows, monkeypatch, db_session) == ["big", "mid", "small"]


# ── 带媒体的请求：只能交给认得出图的候选 ────────────────────────────


def _msg(*blocks):
    return SimpleNamespace(content=list(blocks))


def _build_seeing(primary: _Candidate, *pairs):
    """像 ``_build``，但备选带上 ``extra``——媒体能力就写在这里。"""
    specs = [
        SimpleNamespace(provider_id=c.provider_id, extra={"supports_vision": sees})
        for c, sees in pairs
    ]
    by_pid = {c.provider_id: c for c, _ in pairs}
    return FailoverChatModel(primary, specs, lambda spec: by_pid[spec.provider_id])


_IMAGE = {"type": "data", "source": {"media_type": "image/png"}}


def test_carries_media_sees_uploads_and_tool_results():
    """图可能直接挂在消息上，也可能折在工具回执里——两处都得认出来。"""
    from core.llm.failover import carries_media

    assert carries_media([_msg({"type": "text", "text": "hi"})]) is False
    assert carries_media([_msg(_IMAGE)]) is True
    assert carries_media([_msg({"type": "tool_result", "output": [{"type": "data"}]})]) is True
    assert carries_media([_msg({"type": "tool_result", "output": "done"})]) is False


@pytest.mark.asyncio
async def test_media_request_skips_text_only_fallback():
    """主模型挂了、图还在：只认文字的那家会 400 掀桌，必须跳过它。"""
    primary = _Candidate("primary", ("raise", _status(402)))
    blind = _Candidate("blind", ("value", "should not be called"))
    seeing = _Candidate("seeing", ("value", "described the picture"))
    model = _build_seeing(primary, (blind, False), (seeing, True))

    assert await model._call_api("m", [_msg(_IMAGE)]) == "described the picture"
    assert blind.calls == 0


@pytest.mark.asyncio
async def test_text_request_still_uses_text_only_fallback():
    """没有图的请求不受影响——纯文字的候选照旧顶得上。"""
    primary = _Candidate("primary", ("raise", _status(402)))
    blind = _Candidate("blind", ("value", "answered"))
    model = _build_seeing(primary, (blind, False))

    assert await model._call_api("m", [_msg({"type": "text", "text": "hi"})]) == "answered"


@pytest.mark.asyncio
async def test_media_request_fails_on_the_primary_not_on_a_blind_stand_in():
    """没有别家认图时，报出来的要是主模型的真错，而不是替补对图片的抱怨。

    把图硬塞给只认文字的候选，换来的是一句「参数非法」——它掩盖了真正的原因
    （这里是主模型 402 余额不足），排障的人会往完全错误的方向找。
    """
    primary = _Candidate("primary", ("raise", _status(402)))
    blind = _Candidate("blind", ("value", "answered"))
    model = _build_seeing(primary, (blind, False))

    with pytest.raises(openai.APIStatusError):
        await model._call_api("m", [_msg(_IMAGE)])
    assert blind.calls == 0

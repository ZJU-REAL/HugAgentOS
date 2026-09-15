"""启动补齐：只补没记录过的在用对话上游，探不出来就不写。

补齐存在的意义是让「默认走 Responses」对升级前就建好的模型也成立；但它绝不能把一个
只是暂时连不上的上游错误地钉死在旧协议上，所以无结论时宁可不写、下次启动再试。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.db import model_repository
from core.llm.providers import protocol_probe
from core.llm.providers.protocol_probe import PROTOCOL_CHAT, PROTOCOL_RESPONSES
from core.services.api_protocol_backfill import backfill_missing_api_protocol


def _provider(pid, **kw):
    return SimpleNamespace(
        provider_id=pid,
        provider_type=kw.get("provider_type", "chat"),
        is_active=kw.get("is_active", True),
        provider=kw.get("provider", "openai_compatible"),
        model_name=kw.get("model_name", f"model-{pid}"),
        base_url=kw.get("base_url", "http://up.test/v1"),
        api_key=kw.get("api_key", "k"),
        extra_config=kw.get("extra_config", {}),
    )


@pytest.fixture
def harness(monkeypatch):
    state = {"rows": [], "writes": [], "probed": [], "verdict": PROTOCOL_RESPONSES}

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("core.db.engine.SessionLocal", _Session)
    monkeypatch.setattr(model_repository, "list_providers", lambda db: state["rows"])
    monkeypatch.setattr(
        model_repository,
        "update_provider",
        lambda db, pid, **fields: state["writes"].append((pid, fields)),
    )

    async def _detect(*, base_url, api_key, model_name, timeout=15):
        state["probed"].append(model_name)
        verdict = state["verdict"]
        return protocol_probe.ProtocolProbeResult(
            protocol=verdict or "", status_code=400 if verdict else 0
        )

    monkeypatch.setattr(protocol_probe, "detect_api_protocol", _detect)
    return state


@pytest.mark.asyncio
async def test_records_the_probed_protocol(harness):
    harness["rows"] = [_provider("p1")]

    filled = await backfill_missing_api_protocol()

    assert filled == 1
    pid, fields = harness["writes"][0]
    assert pid == "p1"
    assert fields["extra_config"]["api_protocol"] == PROTOCOL_RESPONSES
    assert fields["extra_config"]["api_protocol_source"] == "probe"


@pytest.mark.asyncio
async def test_an_operators_explicit_choice_is_never_overwritten(harness):
    harness["rows"] = [_provider("p1", extra_config={"api_protocol": PROTOCOL_CHAT})]

    filled = await backfill_missing_api_protocol()

    assert filled == 0
    assert harness["writes"] == []
    assert harness["probed"] == []


@pytest.mark.asyncio
async def test_other_settings_on_the_row_survive_the_write(harness):
    harness["rows"] = [_provider("p1", extra_config={"context_length": 262144})]

    await backfill_missing_api_protocol()

    _, fields = harness["writes"][0]
    assert fields["extra_config"]["context_length"] == 262144


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [
        {"provider_type": "embedding"},
        {"is_active": False},
        {"provider": "azure_openai"},
        {"provider": "anthropic"},
    ],
)
async def test_rows_that_cannot_speak_responses_are_skipped(harness, kw):
    harness["rows"] = [_provider("p1", **kw)]

    filled = await backfill_missing_api_protocol()

    assert filled == 0
    assert harness["probed"] == []


@pytest.mark.asyncio
async def test_inconclusive_probe_writes_nothing(harness):
    harness["rows"] = [_provider("p1")]
    harness["verdict"] = ""

    filled = await backfill_missing_api_protocol()

    assert filled == 0
    assert harness["writes"] == []


@pytest.mark.asyncio
async def test_one_unreachable_upstream_does_not_block_the_others(harness, monkeypatch):
    harness["rows"] = [_provider("p1"), _provider("p2")]

    async def _detect(*, base_url, api_key, model_name, timeout=15):
        if model_name == "model-p1":
            raise RuntimeError("down")
        return protocol_probe.ProtocolProbeResult(
            protocol=PROTOCOL_RESPONSES, status_code=400
        )

    monkeypatch.setattr(protocol_probe, "detect_api_protocol", _detect)

    filled = await backfill_missing_api_protocol()

    assert filled == 1
    assert [pid for pid, _ in harness["writes"]] == ["p2"]

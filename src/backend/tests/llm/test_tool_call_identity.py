"""供应商给的 tool_call id 不可信时，身份在入口处补全（core/llm/tool_call_identity）。

下游一律按 id 认人，所以一个空 id 或一个在同一条响应里重复的 id，就等于让两次并行调用
共用一张卡、互相覆盖结果。这里验证补全规则本身，以及它不会去猜配对。
"""

import asyncio

from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse
from core.models.tool_call_identity import ToolCallIdentity, ToolCallIdentityMixin


def _response(*calls, is_last=True):
    return ChatResponse(
        content=[ToolCallBlock(id=cid, name=name, input=inp) for cid, name, inp in calls],
        is_last=is_last,
    )


def _ids(response):
    return [block.id for block in response.content]


def test_ids_the_provider_got_right_are_left_alone():
    identity = ToolCallIdentity("test/model")
    response = _response(("call_a", "read_image", "{}"), ("call_b", "read_image", "{}"))
    identity.apply(response)
    assert _ids(response) == ["call_a", "call_b"]


def test_an_empty_id_is_given_one():
    identity = ToolCallIdentity("test/model")
    response = _response(("", "bash", '{"cmd":"ls"}'))
    identity.apply(response)
    (call_id,) = _ids(response)
    assert call_id and call_id.startswith("jx-")


def test_the_synthetic_id_is_the_same_one_across_the_stream():
    """增量和最终那条累积响应必须落在同一个 id 上，否则前端会开两张卡。"""
    identity = ToolCallIdentity("test/model")
    delta = _response(("", "bash", '{"cmd":'), is_last=False)
    identity.apply(delta)
    final = _response(("", "bash", '{"cmd":"ls"}'))
    identity.apply(final)
    assert _ids(delta) == _ids(final)


def test_two_empty_id_calls_to_different_tools_stay_apart():
    identity = ToolCallIdentity("test/model")
    response = _response(("", "bash", "{}"), ("", "read_image", "{}"))
    identity.apply(response)
    first, second = _ids(response)
    assert first != second


def test_parallel_same_name_calls_with_no_ids_are_not_merged():
    """身份已经在上游丢了——这里不猜配对，但两次调用绝不能落成同一个 id。"""
    identity = ToolCallIdentity("test/model")
    response = _response(("", "read_image", '{"p":1}'), ("", "read_image", '{"p":2}'))
    identity.apply(response)
    first, second = _ids(response)
    assert first != second


def test_a_reused_id_inside_one_response_is_split():
    identity = ToolCallIdentity("test/model")
    response = _response(("call_a", "read_image", '{"p":1}'), ("call_a", "read_image", '{"p":2}'))
    identity.apply(response)
    first, second = _ids(response)
    assert first == "call_a"
    assert second != first


def test_the_same_id_across_two_deltas_is_the_same_call():
    """流式增量里同一个 id 反复出现是正常的累积，不是重复调用。"""
    identity = ToolCallIdentity("test/model")
    for _ in range(3):
        chunk = _response(("call_a", "bash", "{"), is_last=False)
        identity.apply(chunk)
        assert _ids(chunk) == ["call_a"]


class _FakeModel:
    provider_id = "acme"
    model = "acme-1"

    def __init__(self, result):
        self._result = result

    async def __call__(self, *args, **kwargs):
        return self._result


class _RepairedModel(ToolCallIdentityMixin, _FakeModel):
    pass


def test_the_mixin_repairs_a_single_response():
    response = _response(("", "bash", "{}"))
    result = asyncio.run(_RepairedModel(response)())
    assert _ids(result)[0].startswith("jx-")


def test_the_mixin_repairs_every_chunk_of_a_stream():
    chunks = [_response(("", "bash", "{"), is_last=False), _response(("", "bash", "{}"))]

    async def _stream():
        for chunk in chunks:
            yield chunk

    async def _drain():
        return [_ids(chunk) async for chunk in await _RepairedModel(_stream())()]

    ids = asyncio.run(_drain())
    assert ids[0] == ids[1]
    assert ids[0][0].startswith("jx-")

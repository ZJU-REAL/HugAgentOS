"""State-machine tests for the design_pick (site-design three-way choice) pending gate.

Covers the design_pick branches of _myspace_confirm.pick() and set_decision():
chosen / skipped / invalid option / non-interactive downgrade / allow_session cascade not misfiring.
"""

import asyncio

import pytest

from core.llm.tools import _myspace_confirm as mc

_OPTIONS = [
    {"id": "a", "title": "深色科技风", "image_file_id": "fid_a", "brief": ""},
    {"id": "b", "title": "明亮商务风", "image_file_id": "fid_b", "brief": ""},
    {"id": "c", "title": "极简卡片风", "image_file_id": "fid_c", "brief": ""},
]


def _drain(chat_id: str) -> list:
    q = mc.get_ui_queue(chat_id)
    out = []
    if q is None:
        return out
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


@pytest.mark.asyncio
async def test_pick_chosen():
    chat_id = "chat_dp_chosen"
    task = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="选哪个？", options=_OPTIONS, interactive=True,
    ))
    await asyncio.sleep(0.05)
    signals = _drain(chat_id)
    assert len(signals) == 1
    info = signals[0]
    assert info["kind"] == mc.KIND_DESIGN_PICK
    assert info["question"] == "选哪个？"
    assert [o["id"] for o in info["options"]] == ["a", "b", "c"]

    res = mc.set_decision(chat_id, info["confirm_id"], mc.DECISION_CHOICE, option_id="b")
    assert res["ok"] is True
    out = await asyncio.wait_for(task, timeout=2)
    assert out == {"status": "chosen", "option_id": "b"}


@pytest.mark.asyncio
async def test_pick_skip():
    chat_id = "chat_dp_skip"
    task = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="选哪个？", options=_OPTIONS, interactive=True,
    ))
    await asyncio.sleep(0.05)
    info = _drain(chat_id)[0]
    res = mc.set_decision(chat_id, info["confirm_id"], mc.DECISION_SKIP)
    assert res["ok"] is True
    out = await asyncio.wait_for(task, timeout=2)
    assert out == {"status": "skipped"}


@pytest.mark.asyncio
async def test_pick_rejects_bad_option_and_wrong_kind_decision():
    chat_id = "chat_dp_bad"
    task = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="选哪个？", options=_OPTIONS, interactive=True,
    ))
    await asyncio.sleep(0.05)
    info = _drain(chat_id)[0]
    cid = info["confirm_id"]

    # invalid option_id
    res = mc.set_decision(chat_id, cid, mc.DECISION_CHOICE, option_id="zzz")
    assert res["ok"] is False and res["reason"] == "bad_option"
    # choice without an option_id
    res = mc.set_decision(chat_id, cid, mc.DECISION_CHOICE)
    assert res["ok"] is False and res["reason"] == "bad_option"
    # write-authorization decision value is invalid for design_pick
    res = mc.set_decision(chat_id, cid, mc.DECISION_ALLOW)
    assert res["ok"] is False and res["reason"] == "bad_decision"

    # still pending; a valid decision can still resolve it
    res = mc.set_decision(chat_id, cid, mc.DECISION_CHOICE, option_id="a")
    assert res["ok"] is True
    out = await asyncio.wait_for(task, timeout=2)
    assert out["status"] == "chosen" and out["option_id"] == "a"


@pytest.mark.asyncio
async def test_pick_non_interactive_blocked():
    out = await mc.pick(
        chat_id="chat_dp_ni", question="选哪个？", options=_OPTIONS, interactive=False,
    )
    assert out["status"] == mc.STATUS_BLOCKED


@pytest.mark.asyncio
async def test_concurrent_second_question_rejected():
    """When the same chat already has a pending picker, another question's pick is rejected outright (no concurrent cards)."""
    chat_id = "chat_dp_conc"
    first = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="问题一？", options=_OPTIONS, interactive=True,
    ))
    await asyncio.sleep(0.05)
    out2 = await mc.pick(
        chat_id=chat_id, question="问题二？", options=_OPTIONS, interactive=True,
    )
    assert out2["status"] == "already_pending"
    # the first one is unaffected and can still be selected normally
    info = mc.get_all_pending(chat_id)[0]
    mc.set_decision(chat_id, info["confirm_id"], mc.DECISION_CHOICE, option_id="a")
    out1 = await asyncio.wait_for(first, timeout=2)
    assert out1["status"] == "chosen"


@pytest.mark.asyncio
async def test_cancel_cleans_pending_and_signals_expire():
    """When a run is cancelled, the pending pick must clear its registration and push an expire signal to retract the card."""
    chat_id = "chat_dp_cancel"
    task = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="选哪个？", options=_OPTIONS, interactive=True,
    ))
    await asyncio.sleep(0.05)
    _drain(chat_id)  # consume the card-popup signal
    assert len(mc.get_all_pending(chat_id)) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mc.get_all_pending(chat_id) == []          # registration cleared
    signals = _drain(chat_id)
    assert any(s.get("expired") for s in signals)     # card-retract signal pushed


@pytest.mark.asyncio
async def test_gate_cancel_cleans_pending_too():
    """After gate() and pick() share the same teardown skeleton, a run cancel likewise clears the write-confirm registration and pushes the card-retract signal."""
    chat_id = "chat_gate_cancel"
    task = asyncio.create_task(mc.gate(
        chat_id=chat_id, op=mc.OP_WRITE, logical_path="/myspace/x.txt",
        interactive=True,
    ))
    await asyncio.sleep(0.05)
    _drain(chat_id)
    assert len(mc.get_all_pending(chat_id)) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mc.get_all_pending(chat_id) == []
    assert any(s.get("expired") for s in _drain(chat_id))


@pytest.mark.asyncio
async def test_allow_session_cascade_skips_design_pick():
    """When a myspace write-confirm chooses "allow for this whole session", a design_pick pending in the same chat must not be woken by the cascade."""
    chat_id = "chat_dp_cascade"
    pick_task = asyncio.create_task(mc.pick(
        chat_id=chat_id, question="选哪个？", options=_OPTIONS, interactive=True,
    ))
    gate_task = asyncio.create_task(mc.gate(
        chat_id=chat_id, op=mc.OP_WRITE, logical_path="/myspace/a.txt",
        interactive=True,
    ))
    await asyncio.sleep(0.05)
    signals = _drain(chat_id)
    gate_cid = next(
        s["confirm_id"] for s in signals if s.get("kind") == mc.KIND_MYSPACE
    )

    res = mc.set_decision(chat_id, gate_cid, mc.DECISION_ALLOW_SESSION)
    assert res["ok"] is True
    assert res["cascaded"] == []  # design_pick not cascaded
    assert (await asyncio.wait_for(gate_task, timeout=2)) is None  # write allowed through

    # design_pick is still pending; a normal selection can complete it
    assert not pick_task.done()
    pendings = mc.get_all_pending(chat_id)
    assert len(pendings) == 1 and pendings[0]["kind"] == mc.KIND_DESIGN_PICK
    res = mc.set_decision(
        chat_id, pendings[0]["confirm_id"], mc.DECISION_CHOICE, option_id="c",
    )
    assert res["ok"] is True
    out = await asyncio.wait_for(pick_task, timeout=2)
    assert out == {"status": "chosen", "option_id": "c"}

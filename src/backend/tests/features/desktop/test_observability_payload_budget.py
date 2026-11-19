"""Observation payloads shrink to the event limit and always terminate."""
from __future__ import annotations

import json
import threading

from features.desktop.observability import projection


def _call_with_deadline(fn, seconds: float = 10.0):
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "public_payload did not finish"
    return box["value"]


def test_oversized_field_is_cut_and_ids_are_kept():
    # scrub() already caps each string at 100k characters, so it takes two
    # large CJK fields to pass the event limit; only the larger one is cut.
    payload = {
        "id": "t1",
        "chat_id": "c1",
        "message_id": "m1",
        "tool_name": "kb",
        "tool_args": "参数" * 45_000,
        "tool_result": "检索" * 300_000,
    }
    result = _call_with_deadline(lambda: projection.public_payload("tool", payload))
    assert result["truncated"] is True
    assert result["chat_id"] == "c1" and result["message_id"] == "m1"
    assert result["tool_result"].endswith("\n[truncated]")
    assert result["tool_args"] == "参数" * 45_000
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= projection.MAX_EVENT_BYTES - 4096


def test_payload_that_cannot_reach_the_limit_still_returns():
    # Every field already at the 8192-character cut size in 4-byte characters:
    # cutting the largest one again changes nothing, which used to spin forever.
    fields = projection.FIELDS["tool"].split()
    payload = {k: "😀" * 9000 for k in fields}
    result = _call_with_deadline(lambda: projection.public_payload("tool", payload))
    assert result["truncated"] is True
    assert all(v.endswith("\n[truncated]") for k, v in result.items() if k not in {"chat_id", "message_id", "truncated"})

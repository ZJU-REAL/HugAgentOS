"""Compaction must hand JSON-serializable rows to the checkpoint writer.

HookBus freezes invocation data into MappingProxyType so consumers cannot
mutate it. A shallow ``list()`` / ``dict()`` copy leaves those proxies nested
inside, and the JSONB checkpoint write then fails with "Object of type
mappingproxy is not JSON serializable" — silently disabling compaction.
"""

from __future__ import annotations

import json

from core.harness.events import thaw_value
from core.harness.hooks import HookStage, Invocation


def _invocation(data):
    return Invocation.create(
        run_id="run-1",
        stage=HookStage.AFTER_COMPACTION,
        operation_name="context_compaction",
        data=data,
    )


def test_shallow_copy_of_hook_data_is_not_serializable():
    inv = _invocation({"replacement": [{"role": "user", "content": "hi"}]})
    shallow = list(inv.data["replacement"])
    try:
        json.dumps(shallow)
    except TypeError as exc:
        assert "mappingproxy" in str(exc)
    else:
        raise AssertionError("expected the shallow copy to stay frozen")


def test_thawed_hook_data_round_trips_through_json():
    rows = [{"role": "user", "content": "hi", "meta": {"tags": ["a"]}}]
    inv = _invocation({"replacement": rows, "budget": {"nested": {"k": "v"}}})

    replacement = thaw_value(inv.data["replacement"])
    budget = thaw_value(inv.data["budget"])

    assert json.loads(json.dumps(replacement)) == rows
    assert json.loads(json.dumps(budget)) == {"nested": {"k": "v"}}
    assert type(replacement[0]) is dict
    assert type(budget["nested"]) is dict

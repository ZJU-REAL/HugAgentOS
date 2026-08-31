"""Harness 4.8 canonical context contracts (no AgentScope dependency)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from core.llm.context_ir import ContextAssembler, ContextItem


def _item(
    item_id: str,
    content,
    *,
    kind: str = "user_input",
    origin: str = "user:chat",
    trust: str = "user",
    priority: int = 500,
    token_budget: int = 100,
    policy: str = "head_tail",
    created_seq: int = 1,
    role: str = "user",
    pair_id: str = "",
    visibility: str = "model",
):
    return ContextItem.create(
        item_id=item_id,
        kind=kind,
        origin=origin,
        trust=trust,
        visibility=visibility,
        priority=priority,
        token_budget=token_budget,
        truncation_policy=policy,
        content=content,
        cache_class="dynamic",
        created_seq=created_seq,
        render_role=role,
        pair_id=pair_id,
        message_group=f"message:{created_seq}",
    )


def test_context_ir_module_does_not_import_agentscope():
    # The adapter owns framework types; budgeting/provenance remains portable.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import core.llm.context_ir; "
                "raise SystemExit('agentscope.message' in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_framework_neutral_fake_adapter_drives_canonical_assembly_and_rendering():
    class FakeAdapter:
        def items_from_messages(
            self,
            messages,
            *,
            summary_text=None,
            promote_latest_user=True,
        ):
            del summary_text, promote_latest_user
            return [
                _item(
                    row["id"],
                    row["text"],
                    created_seq=row["seq"],
                    priority=row.get("priority", 500),
                )
                for row in messages
            ]

        def messages_from_items(self, items):
            return [{"id": item.item_id, "text": str(item.content)} for item in items]

        def reference_items_from_execution_manifest(self, manifest):
            del manifest
            return []

    adapter = FakeAdapter()
    candidates = adapter.items_from_messages(
        [
            {"id": "later", "text": "second", "seq": 2},
            {"id": "earlier", "text": "first", "seq": 1},
        ]
    )
    assembly = ContextAssembler(total_budget=100).assemble(candidates)

    # Context order wins: a lower sequence does not jump the queue.
    assert adapter.messages_from_items(assembly.included) == [
        {"id": "later", "text": "second"},
        {"id": "earlier", "text": "first"},
    ]


def test_assembly_preserves_context_order_regardless_of_sequence():
    """The assembler selects and truncates; it must never reorder.

    Sorting by sequence here moved the live user instruction — parked 64 strides
    above the context tail — behind the whole turn it had just started, and the
    model re-ran that turn as if the request were new.
    """

    items = [
        _item("u2", "second", created_seq=20),
        _item(
            "system",
            "critical rules",
            kind="system_rule",
            origin="platform:system",
            trust="platform",
            priority=1000,
            policy="never",
            created_seq=0,
            role="system",
        ),
        _item("u1", "first", created_seq=10),
    ]

    left = ContextAssembler(total_budget=500).assemble(items)
    right = ContextAssembler(total_budget=500).assemble(list(reversed(items)))

    assert [item.item_id for item in left.included] == ["u2", "system", "u1"]
    assert [item.item_id for item in right.included] == ["u1", "system", "u2"]

    # The audit guarantee the canonical sort was introduced for: one input,
    # one hash. Reproducibility never required reordering the conversation.
    assert ContextAssembler(total_budget=500).assemble(items).manifest_hash == (
        left.manifest_hash
    )


def test_low_trust_content_cannot_displace_critical_system_item():
    system = _item(
        "system",
        "S" * 80,
        kind="system_rule",
        origin="platform:system",
        trust="platform",
        priority=1000,
        token_budget=100,
        policy="never",
        created_seq=0,
        role="system",
    )
    untrusted = _item(
        "external",
        "E" * 400,
        kind="project_material",
        origin="external:file",
        trust="untrusted",
        priority=10,
        token_budget=200,
        policy="drop",
        created_seq=1,
    )

    assembly = ContextAssembler(total_budget=45).assemble([untrusted, system])

    assert [item.item_id for item in assembly.included] == ["system"]
    assert assembly.excluded[0].item_id == "external"
    assert assembly.manifest["excluded"][0]["reason"] == "budget"


def test_item_budget_applies_explicit_head_tail_truncation_and_explains_it():
    long_memory = _item(
        "memory",
        "head-" + "x" * 400 + "-tail",
        kind="memory",
        origin="memory:l2",
        trust="memory",
        priority=700,
        token_budget=30,
        policy="head_tail",
    )
    assembly = ContextAssembler(total_budget=100).assemble([long_memory])

    rendered = str(assembly.included[0].content)
    assert rendered.startswith("head-")
    assert rendered.endswith("-tail")
    assert "omitted" in rendered
    entry = assembly.manifest["included"][0]
    assert entry["action"] == "truncated"
    assert entry["original_tokens"] > entry["final_tokens"]


def test_tool_call_and_result_are_kept_or_excluded_as_one_pair():
    call = _item(
        "call:t1",
        {"type": "tool_call", "id": "t1", "name": "read", "input": "{}"},
        kind="tool_call",
        origin="agent:tool_call",
        trust="assistant",
        priority=700,
        token_budget=100,
        policy="never",
        created_seq=2,
        role="assistant",
        pair_id="t1",
    )
    result = _item(
        "result:t1",
        {"type": "tool_result", "id": "t1", "name": "read", "output": "R" * 600},
        kind="tool_result",
        origin="tool:read",
        trust="tool",
        priority=700,
        token_budget=200,
        policy="head_tail",
        created_seq=3,
        role="assistant",
        pair_id="t1",
    )

    fits = ContextAssembler(total_budget=90).assemble([call, result])
    assert {item.kind for item in fits.included} == {"tool_call", "tool_result"}
    assert fits.included[1].content["output"].endswith("R" * 10)

    dropped = ContextAssembler(total_budget=1).assemble([call, result])
    assert dropped.included == ()
    assert {entry["reason"] for entry in dropped.manifest["excluded"]} == {"paired_budget"}


def test_structured_tool_result_output_stays_a_tool_result_when_truncated():
    call = _item(
        "call:t2",
        {"type": "tool_call", "id": "t2", "name": "read", "input": "{}"},
        kind="tool_call",
        origin="agent:tool_call",
        trust="assistant",
        priority=700,
        token_budget=100,
        policy="never",
        created_seq=2,
        role="assistant",
        pair_id="t2",
    )
    result = _item(
        "result:t2",
        {
            "type": "tool_result",
            "id": "t2",
            "name": "read",
            "output": [{"type": "text", "text": "R" * 800}],
        },
        kind="tool_result",
        origin="tool:read",
        trust="tool",
        priority=700,
        token_budget=40,
        policy="head_tail",
        created_seq=3,
        role="assistant",
        pair_id="t2",
    )

    assembly = ContextAssembler(total_budget=100).assemble([call, result])
    truncated = next(item for item in assembly.included if item.kind == "tool_result")

    assert truncated.content["type"] == "tool_result"
    assert isinstance(truncated.content["output"], str)
    assert "omitted" in truncated.content["output"]


def test_orphan_tool_item_is_never_rendered():
    orphan = _item(
        "result:orphan",
        {"type": "tool_result", "id": "orphan", "output": "no call"},
        kind="tool_result",
        pair_id="orphan",
        role="assistant",
    )
    assembly = ContextAssembler(total_budget=100).assemble([orphan])

    assert assembly.included == ()
    assert assembly.manifest["excluded"][0]["reason"] == "malformed_tool_pair"


def test_tool_pair_multiplicity_is_rejected_as_malformed():
    call_one = _item(
        "call:one",
        {"type": "tool_call", "id": "same", "name": "read", "input": "{}"},
        kind="tool_call",
        pair_id="same",
        role="assistant",
    )
    call_two = _item(
        "call:two",
        {"type": "tool_call", "id": "same", "name": "read", "input": "{}"},
        kind="tool_call",
        pair_id="same",
        role="assistant",
        created_seq=2,
    )
    result = _item(
        "result",
        {"type": "tool_result", "id": "same", "name": "read", "output": "done"},
        kind="tool_result",
        pair_id="same",
        role="assistant",
        created_seq=3,
    )

    assembly = ContextAssembler(total_budget=1_000).assemble([call_one, result, call_two])

    assert assembly.included == ()
    assert {entry["reason"] for entry in assembly.manifest["excluded"]} == {"malformed_tool_pair"}


def test_memory_and_reminder_keep_compat_role_but_not_user_provenance():
    memory = _item(
        "memory",
        "remember this",
        kind="memory",
        origin="memory:frozen",
        trust="memory",
        role="user",
    )
    reminder = _item(
        "reminder",
        "finish now",
        kind="reminder",
        origin="harness:iteration_budget",
        trust="system",
        role="user",
    )

    # Fake adapter: no AgentScope import needed to prove compatibility role and
    # machine-readable provenance are independent fields.
    rendered = [(item.render_role, item.origin, item.trust) for item in (memory, reminder)]

    assert rendered == [
        ("user", "memory:frozen", "memory"),
        ("user", "harness:iteration_budget", "system"),
    ]


def test_duplicate_item_ids_are_rejected_before_manifest_records_can_alias():
    left = _item("duplicate", "A", created_seq=1)
    right = _item("duplicate", "B", created_seq=2)

    with pytest.raises(ValueError, match="item_id values must be unique"):
        ContextAssembler(total_budget=100).assemble([left, right])


def test_data_block_is_dropped_whole_instead_of_becoming_truncated_json_text():
    image = _item(
        "image",
        {
            "type": "data",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "A" * 4_000,
            },
        },
        kind="attachment",
        token_budget=2_000,
        policy="head_tail",
    )

    assembly = ContextAssembler(total_budget=1_000).assemble([image])

    assert image.truncation_policy == "drop"
    assert assembly.included == ()
    assert assembly.manifest["excluded"][0]["reason"] == "budget"


def test_manifest_is_sanitized_and_content_hash_sensitive():
    item = _item("private", "do not persist this plaintext")
    changed = _item("private", "do not persist this plaintexT")
    first = ContextAssembler(total_budget=100).assemble([item])
    second = ContextAssembler(total_budget=100).assemble([changed])

    encoded = json.dumps(first.manifest, ensure_ascii=False)
    assert "do not persist" not in encoded
    assert first.manifest_hash != second.manifest_hash

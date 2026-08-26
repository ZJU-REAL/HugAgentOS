"""Harness 4.7 execution-manifest contracts."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from core.llm.execution_manifest import (
    PromptManifestBuilder,
    stable_hash,
    stable_tool_schema_order,
)


def _tool_schema(*, enum_order=("fast", "safe")):
    return {
        "type": "function",
        "function": {
            "name": "inspect_workspace",
            "description": "Inspect the active workspace without changing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": list(enum_order)},
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    }


def _manifest(*, context=None, schema=None):
    builder = PromptManifestBuilder(context=context or {})
    builder.add_prompt_section(
        "system/base",
        "You are a careful assistant.",
        origin="prompt-pack:system",
        trust="platform",
        priority=10,
        cache_class="stable_prefix",
        budget=2048,
        version="v7",
    )
    builder.add_prompt_section(
        "runtime/project",
        "secret project instruction after the stable prefix",
        origin="workspace:project-1",
        trust="workspace",
        priority=900,
        cache_class="workspace",
        budget=512,
        version="project-revision-2",
        reference="project:project-1",
        sensitive=True,
    )
    builder.add_tool_definition(
        schema or _tool_schema(),
        origin="builtin",
        trust="platform",
        priority=100,
        cache_class="stable",
        version="1",
        permission_policy={"decision": "allow", "source": "jx_trusted"},
        recovery_policy={"strategy": "runtime_default"},
    )
    return builder.build(
        final_prompt=(
            "You are a careful assistant.\n\n" "secret project instruction after the stable prefix"
        )
    )


def test_canonical_hash_is_stable_across_mapping_order():
    left = {"b": [2, {"z": True, "a": None}], "a": 1}
    right = {"a": 1, "b": [2, {"a": None, "z": True}]}
    assert stable_hash(left) == stable_hash(right)


def test_provider_tool_schema_order_is_stable_by_name():
    schemas = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    names = [item["function"]["name"] for item in stable_tool_schema_order(reversed(schemas))]
    assert names == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_agent_skill_prompt_order_is_stable_by_name():
    from agentscope.skill import Skill
    from core.ontology.toolkit import OntologyFilteredToolkit

    toolkit = OntologyFilteredToolkit(
        skills_or_loaders=[
            Skill("zeta", "Zeta skill", "/skills/zeta", "zeta", 2.0),
            Skill("alpha", "Alpha skill", "/skills/alpha", "alpha", 1.0),
        ]
    )
    instructions = await toolkit.get_skill_instructions()
    assert instructions.index("alpha") < instructions.index("zeta")


def test_manifest_records_section_contract_without_sensitive_plaintext():
    manifest = _manifest(context={"workspace_id": "ws-1", "project": {"secret": "omega"}})
    payload = manifest.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)

    section = payload["prompt_manifest"]["sections"][1]
    assert section["id"] == "runtime/project"
    assert section["origin"] == "workspace:project-1"
    assert section["trust"] == "workspace"
    assert section["priority"] == 900
    assert section["cache_class"] == "workspace"
    assert section["budget"] == 512
    assert section["version"] == "project-revision-2"
    assert section["token_estimate"] > 0
    assert len(section["content_hash"]) == 64
    assert "secret project instruction" not in encoded
    assert "omega" not in encoded


def test_tool_manifest_hashes_name_description_schema_permission_and_recovery():
    baseline = _manifest()
    reordered = _manifest(
        schema={
            "function": {
                "parameters": {
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "mode": {"enum": ["fast", "safe"], "type": "string"},
                    },
                    "type": "object",
                },
                "description": "Inspect the active workspace without changing it.",
                "name": "inspect_workspace",
            },
            "type": "function",
        }
    )
    changed = _manifest(schema=_tool_schema(enum_order=("safe", "fast")))

    assert baseline.tool_manifest_hash == reordered.tool_manifest_hash
    assert baseline.aggregate_hash == reordered.aggregate_hash
    assert baseline.tool_manifest_hash != changed.tool_manifest_hash
    assert baseline.prompt_hash == changed.prompt_hash
    assert baseline.context_hash == changed.context_hash


def test_changing_one_prompt_section_only_changes_prompt_side_hashes():
    baseline = _manifest(context={"workspace_id": "ws-1"})
    builder = PromptManifestBuilder(context={"workspace_id": "ws-1"})
    builder.add_prompt_section(
        "system/base",
        "You are a different careful assistant.",
        origin="prompt-pack:system",
        trust="platform",
        priority=10,
        cache_class="stable_prefix",
        budget=2048,
        version="v8",
    )
    builder.add_prompt_section(
        "runtime/project",
        "secret project instruction after the stable prefix",
        origin="workspace:project-1",
        trust="workspace",
        priority=900,
        cache_class="workspace",
        budget=512,
        version="project-revision-2",
        reference="project:project-1",
        sensitive=True,
    )
    builder.add_tool_definition(
        _tool_schema(),
        origin="builtin",
        trust="platform",
        priority=100,
        cache_class="stable",
        version="1",
        permission_policy={"decision": "allow", "source": "jx_trusted"},
        recovery_policy={"strategy": "runtime_default"},
    )
    changed = builder.build(
        final_prompt=(
            "You are a different careful assistant.\n\n"
            "secret project instruction after the stable prefix"
        )
    )

    assert baseline.prompt_hash != changed.prompt_hash
    assert baseline.prompt_manifest_hash != changed.prompt_manifest_hash
    assert baseline.aggregate_hash != changed.aggregate_hash
    assert baseline.tool_manifest_hash == changed.tool_manifest_hash
    assert baseline.context_hash == changed.context_hash


def test_context_hash_uses_full_nested_content_but_persists_only_hashes_and_refs():
    prefix = "x" * 200
    one = _manifest(
        context={
            "workspace_id": "ws-1",
            "project_id": "project-1",
            "project_instructions": prefix + "A",
            "project_files": [{"name": "private-a.txt", "size_bytes": 10}],
        }
    )
    two = _manifest(
        context={
            "project_files": [{"size_bytes": 10, "name": "private-a.txt"}],
            "project_instructions": prefix + "B",
            "project_id": "project-1",
            "workspace_id": "ws-1",
        }
    )

    assert one.context_hash != two.context_hash
    persisted = json.dumps(one.to_dict(), ensure_ascii=False)
    assert prefix not in persisted
    assert "private-a.txt" not in persisted
    assert "ws-1" in persisted
    assert "project-1" in persisted


def test_execution_manifest_context_refs_reject_direct_nested_mutation():
    manifest = _manifest(
        context={
            "workspace_id": "ws-1",
            "project": {"files": ["private-a.txt"]},
        }
    )
    original = manifest.to_dict()

    with pytest.raises(TypeError):
        manifest.context_refs["workspace_id"] = "tampered"
    with pytest.raises(TypeError):
        manifest.context_refs["project"]["content_hash"] = "tampered"

    assert manifest.to_dict() == original


def test_final_context_manifest_is_immutable_sanitized_and_part_of_aggregate_hash():
    base = _manifest(context={"workspace_id": "ws-1"})
    context_manifest = {
        "schema_version": "harness.context.v1",
        "included": [
            {
                "item_id": "user:1",
                "content_hash": "a" * 64,
                "origin": "user:chat",
                "trust": "user",
            }
        ],
        "excluded": [],
    }

    request = base.with_context_manifest(context_manifest)
    encoded = json.dumps(request.to_dict(), ensure_ascii=False)

    assert request.base_context_hash == base.context_hash
    assert request.context_manifest_hash == stable_hash(context_manifest)
    assert request.context_hash != base.context_hash
    assert request.aggregate_hash != base.aggregate_hash
    assert request.to_dict()["context_manifest"] == context_manifest
    assert "plaintext user request" not in encoded
    with pytest.raises(TypeError):
        request.context_manifest["included"][0]["origin"] = "tampered"


def test_aggregate_hash_is_stable_across_fresh_python_processes():
    script = r"""
import json
import sys
from core.llm.execution_manifest import PromptManifestBuilder

ordered = sys.argv[1] == "ordered"
context = (
    {"workspace_id": "ws-1", "project": {"a": 1, "b": [2, 3]}}
    if ordered
    else {"project": {"b": [2, 3], "a": 1}, "workspace_id": "ws-1"}
)
builder = PromptManifestBuilder(context=context)
builder.add_prompt_section(
    "system/base", "same prompt", origin="test", trust="platform",
    priority=10, cache_class="stable", version="1",
)
builder.add_tool_definition(
    {
        "type": "function",
        "function": {
            "name": "inspect",
            "description": "Inspect",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    origin="builtin", trust="platform", priority=10, cache_class="stable",
)
print(builder.build(final_prompt="same prompt").aggregate_hash)
"""
    first = subprocess.check_output([sys.executable, "-c", script, "ordered"], text=True).strip()
    second = subprocess.check_output([sys.executable, "-c", script, "reordered"], text=True).strip()

    assert first == second


def test_golden_manifest_snapshot():
    manifest = _manifest(context={"workspace_id": "ws-1", "project_id": "project-1"})
    assert manifest.schema_version == "harness.execution-manifest.v1"
    assert (
        manifest.aggregate_hash
        == "6673d5a0ed937252f0c7d836e7243e0ee4969df2a0ce418171f8db0cb8c1cf56"
    )

"""GCE ticket 03 — Runtime Binder and asset bundles.

The properties under test are the ones the rest of the evolution loop leans on:
a bundle must not drift once bound, single-asset substitution must leave
everything else untouched, and binding must never be able to fail a turn.
"""

import pytest

from core.evolution import contract as C
from core.evolution import runtime_binding as rb


@pytest.fixture(autouse=True)
def _clean_binder():
    rb.reset_for_tests()
    yield
    rb.reset_for_tests()


def _ref(kind, asset_id, version="v1"):
    return C.AssetRef(kind=kind, asset_id=asset_id, version=version)


# ── bundle identity ──────────────────────────────────────────────────────────


def test_bundle_id_is_order_independent():
    a = C.build_bundle([_ref(C.ASSET_SKILL, "s1"), _ref(C.ASSET_SKILL, "s2")])
    b = C.build_bundle([_ref(C.ASSET_SKILL, "s2"), _ref(C.ASSET_SKILL, "s1")])
    # Loader ordering must never read as "the configuration changed".
    assert a.bundle_id == b.bundle_id


def test_bundle_id_changes_when_a_version_changes():
    a = C.build_bundle([_ref(C.ASSET_SKILL, "s1", "v1")])
    b = C.build_bundle([_ref(C.ASSET_SKILL, "s1", "v2")])
    assert a.bundle_id != b.bundle_id


def test_duplicate_refs_are_collapsed():
    bundle = C.build_bundle([_ref(C.ASSET_SKILL, "s1"), _ref(C.ASSET_SKILL, "s1")])
    assert len(bundle.refs) == 1


def test_refs_without_asset_id_are_dropped():
    bundle = C.build_bundle([C.AssetRef(kind=C.ASSET_SKILL, asset_id="", version="v1")])
    assert bundle.refs == ()


def test_bundle_roundtrips_through_dict():
    original = C.build_bundle(
        [_ref(C.ASSET_SKILL, "s1"), _ref(C.ASSET_PROMPT, "system", "p7")], partial=True
    )
    restored = C.AssetBundle.from_dict(original.to_dict())
    assert restored.bundle_id == original.bundle_id
    assert restored.partial is True
    assert {r.key() for r in restored.refs} == {r.key() for r in original.refs}


# ── single-variable substitution (the replay primitive) ──────────────────────


def test_replace_ref_swaps_exactly_one_asset():
    bundle = C.build_bundle(
        [
            _ref(C.ASSET_SKILL, "s1", "v1"),
            _ref(C.ASSET_SKILL, "s2", "v1"),
            _ref(C.ASSET_PROMPT, "system", "p1"),
        ]
    )
    swapped = bundle.replace_ref(_ref(C.ASSET_SKILL, "s1", "v9"))

    assert swapped.first_of_kind(C.ASSET_PROMPT).version == "p1"
    versions = {r.asset_id: r.version for r in swapped.of_kind(C.ASSET_SKILL)}
    assert versions == {"s1": "v9", "s2": "v1"}
    # Everything else identical ⇒ any measured difference is attributable.
    assert len(swapped.refs) == len(bundle.refs)


def test_replace_ref_adds_when_asset_absent():
    bundle = C.build_bundle([_ref(C.ASSET_SKILL, "s1")])
    swapped = bundle.replace_ref(_ref(C.ASSET_WORKFLOW, "wf1"))
    assert swapped.first_of_kind(C.ASSET_WORKFLOW) is not None


def test_without_removes_a_single_asset():
    bundle = C.build_bundle([_ref(C.ASSET_SKILL, "s1"), _ref(C.ASSET_SKILL, "s2")])
    reduced = bundle.without(C.ASSET_SKILL, "s1")
    # Removal is a different counterfactual from substitution — "what if this had
    # never been loaded" vs "what if it had said something else".
    assert [r.asset_id for r in reduced.of_kind(C.ASSET_SKILL)] == ["s2"]


def test_substitution_does_not_mutate_the_original():
    bundle = C.build_bundle([_ref(C.ASSET_SKILL, "s1", "v1")])
    bundle.replace_ref(_ref(C.ASSET_SKILL, "s1", "v2"))
    assert bundle.first_of_kind(C.ASSET_SKILL).version == "v1"


def test_substitution_invalidates_an_execution_manifest_until_rebuilt():
    original = C.build_bundle(
        [_ref(C.ASSET_SKILL, "s1", "v1")],
        execution_manifest={"aggregate_hash": "manifest-v1"},
    )
    substituted = original.replace_ref(_ref(C.ASSET_SKILL, "s1", "v2"))
    removed = original.without(C.ASSET_SKILL, "s1")

    for candidate in (substituted, removed):
        assert candidate.partial is True
        assert candidate.execution_manifest == {}


def test_bundle_manifest_is_not_mutable_through_input_or_to_dict_aliases():
    source = {
        "aggregate_hash": "manifest-v1",
        "context_refs": {"workspace_id": "ws-1"},
    }
    bundle = C.build_bundle([_ref(C.ASSET_SKILL, "s1")], execution_manifest=source)
    original_id = bundle.bundle_id

    source["context_refs"]["workspace_id"] = "mutated-input"
    exported = bundle.to_dict()
    exported["execution_manifest"]["context_refs"]["workspace_id"] = "mutated-output"

    assert bundle.bundle_id == original_id
    assert bundle.execution_manifest["context_refs"]["workspace_id"] == "ws-1"


def test_bundle_manifest_and_ref_details_reject_direct_nested_mutation():
    bundle = C.build_bundle(
        [
            C.AssetRef(
                kind=C.ASSET_SKILL,
                asset_id="s1",
                detail={"policy": {"allowed": ["read"]}},
            )
        ],
        execution_manifest={
            "aggregate_hash": "manifest-v1",
            "context_refs": {"workspace_id": "ws-1"},
        },
    )
    original_id = bundle.bundle_id

    with pytest.raises(TypeError):
        bundle.execution_manifest["aggregate_hash"] = "tampered"
    with pytest.raises(TypeError):
        bundle.execution_manifest["context_refs"]["workspace_id"] = "tampered"
    with pytest.raises(TypeError):
        bundle.refs[0].detail["policy"]["allowed"][0] = "write"

    assert bundle.bundle_id == original_id
    assert C.compute_bundle_id(list(bundle.refs), bundle.execution_manifest) == original_id


def test_memory_policy_ref_names_each_layer():
    """Attribution must distinguish "L3 offered nothing" from "L3 was off".

    A single opaque ``v1`` policy could not answer which layered configuration
    a given Episode actually ran under.
    """
    refs = rb._memory_refs(True, "ws-1")
    layers = refs[0].detail["layers"]
    assert set(layers) == {"profile", "fact", "graph"}
    assert "enabled" in layers["graph"]
    assert "dedup_min_score" in layers["fact"]

    # With the user switch off, every layer reads disabled regardless of env.
    off = rb._memory_refs(False, "ws-1")
    assert off[0].detail["enabled"] is False
    assert off[0].detail["layers"]["graph"]["enabled"] is False


# ── binding behaviour ────────────────────────────────────────────────────────


def test_binding_never_raises_when_every_resolver_fails(monkeypatch):
    for name in (
        "_prompt_refs",
        "_ontology_refs",
        "_memory_refs",
        "_workflow_refs",
    ):
        monkeypatch.setattr(rb, name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        rb, "_skill_refs", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    bundle = rb.bind_runtime_assets(run_id="run-1", skill_ids=["s1"])
    # Bookkeeping must never become an availability risk.
    assert bundle is not None
    assert bundle.partial is True


def test_partial_flag_set_when_any_resolver_degrades(monkeypatch):
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: (_ for _ in ()).throw(RuntimeError("nope")))
    bundle = rb.bind_runtime_assets(run_id="run-2", skill_ids=["s1"])
    assert bundle.partial is True


def test_bundle_is_retrievable_by_run_id(monkeypatch):
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [_ref(C.ASSET_SKILL, "s1")])

    bundle = rb.bind_runtime_assets(run_id="run-3", skill_ids=["s1"])
    assert rb.resolve_bundle_for_run("run-3").bundle_id == bundle.bundle_id
    assert rb.get_bundle(bundle.bundle_id) is not None


def test_bundle_for_run_keeps_workspace_policy_and_sanitized_execution_manifest(monkeypatch):
    from core.llm.execution_manifest import PromptManifestBuilder

    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [])
    manifest_builder = PromptManifestBuilder(
        context={
            "workspace_id": "workspace-7",
            "project_instructions": "do not persist this plaintext",
        }
    )
    manifest_builder.add_prompt_section(
        "system/base",
        "private system prompt",
        origin="prompt-pack:system",
        trust="platform",
        priority=10,
        cache_class="stable_prefix",
        version="v1",
        sensitive=True,
    )
    manifest = manifest_builder.build(final_prompt="private system prompt")

    bundle = rb.bind_runtime_assets(
        run_id="run-workspace-7",
        workspace_id="workspace-7",
        execution_manifest=manifest,
    )
    restored = rb.resolve_bundle_for_run("run-workspace-7")

    assert restored is not None
    policy = restored.first_of_kind(C.ASSET_MEMORY)
    assert policy is not None and policy.asset_id == "policy:workspace-7"
    assert restored.execution_manifest["aggregate_hash"] == manifest.aggregate_hash
    assert "private system prompt" not in str(restored.to_dict())
    assert "do not persist this plaintext" not in str(restored.to_dict())


def test_manifest_required_marks_missing_or_failed_manifest_partial(monkeypatch):
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [])

    missing = rb.bind_runtime_assets(run_id="run-missing", manifest_required=True)

    class BrokenManifest:
        def to_dict(self):
            raise RuntimeError("serialization failed")

    broken = rb.bind_runtime_assets(
        run_id="run-broken",
        execution_manifest=BrokenManifest(),
        manifest_required=True,
    )

    assert missing.partial is True
    assert broken.partial is True
    assert missing.execution_manifest == {}
    assert broken.execution_manifest == {}


def test_binder_uses_rendered_prompt_versions_not_a_new_active_pointer(monkeypatch):
    from core.llm.execution_manifest import PromptManifestBuilder

    monkeypatch.setattr(
        rb,
        "_prompt_refs",
        lambda: (_ for _ in ()).throw(AssertionError("must not re-read active prompt")),
    )
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [])
    builder = PromptManifestBuilder(context={"workspace_id": "ws-race"})
    builder.add_prompt_section(
        "system/base",
        "rendered before concurrent publish",
        origin="prompt-version:system-v1",
        trust="admin",
        priority=10,
        cache_class="stable_prefix",
        version="system-v1",
    )
    manifest = builder.build(final_prompt="rendered before concurrent publish")

    bundle = rb.bind_runtime_assets(
        run_id="run-prompt-race",
        execution_manifest=manifest,
        manifest_required=True,
    )

    prompt_refs = bundle.of_kind(C.ASSET_PROMPT)
    assert bundle.partial is False
    assert [(ref.asset_id, ref.version) for ref in prompt_refs] == [("system/base", "system-v1")]
    assert prompt_refs[0].detail["content_hash"] == manifest.prompt_sections[0].content_hash


def test_no_drift_when_assets_republish_after_binding(monkeypatch):
    """The core invariant: a mid-run publish must not change this run."""
    live_version = {"v": "v1"}
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(
        rb, "_skill_refs", lambda ids: [_ref(C.ASSET_SKILL, "s1", live_version["v"])]
    )

    bound = rb.bind_runtime_assets(run_id="run-4", skill_ids=["s1"])
    live_version["v"] = "v2"  # admin publishes mid-run

    still = rb.resolve_bundle_for_run("run-4")
    assert still.first_of_kind(C.ASSET_SKILL).version == "v1"
    assert still.bundle_id == bound.bundle_id


def test_request_manifest_rebind_preserves_frozen_refs_and_updates_run_index(monkeypatch):
    from core.llm.execution_manifest import PromptManifestBuilder

    live_version = {"v": "v1"}
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(
        rb,
        "_skill_refs",
        lambda ids: [_ref(C.ASSET_SKILL, "s1", live_version["v"])],
    )
    builder = PromptManifestBuilder(context={"workspace_id": "ws-context"})
    base_manifest = builder.build(final_prompt="system")
    base_bundle = rb.bind_runtime_assets(
        run_id="run-context",
        skill_ids=["s1"],
        execution_manifest=base_manifest,
    )
    live_version["v"] = "v2"
    request_manifest = base_manifest.with_context_manifest(
        {"schema_version": "harness.context.v1", "included": [], "excluded": []}
    )

    rebound = rb.rebind_execution_manifest(
        run_id="run-context",
        base_bundle=base_bundle,
        execution_manifest=request_manifest,
    )

    assert rebound.first_of_kind(C.ASSET_SKILL).version == "v1"
    assert rebound.execution_manifest["context_manifest_hash"]
    assert rb.resolve_bundle_for_run("run-context").bundle_id == rebound.bundle_id


def test_model_ref_captured_so_runs_across_providers_are_comparable(monkeypatch):
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [])

    bundle = rb.bind_runtime_assets(
        run_id="run-5", model_name="some-model", model_provider_id="prov-1"
    )
    model = bundle.first_of_kind(C.ASSET_MODEL)
    # Without this, two runs on different providers look identical and every
    # comparison between them is silently confounded.
    assert model is not None and model.version == "some-model"


def test_checksum_is_stable_and_content_sensitive():
    a = C.build_bundle([_ref(C.ASSET_SKILL, "s1")])
    b = C.build_bundle([_ref(C.ASSET_SKILL, "s1")])
    c = C.build_bundle([_ref(C.ASSET_SKILL, "s2")])
    assert C.bundle_checksum(a) == C.bundle_checksum(b)
    assert C.bundle_checksum(a) != C.bundle_checksum(c)

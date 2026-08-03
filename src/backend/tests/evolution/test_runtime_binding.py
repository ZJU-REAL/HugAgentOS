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


# ── binding behaviour ────────────────────────────────────────────────────────


def test_binding_never_raises_when_every_resolver_fails(monkeypatch):
    for name in (
        "_prompt_refs",
        "_ontology_refs",
        "_memory_refs",
        "_workflow_refs",
    ):
        monkeypatch.setattr(
            rb, name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
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
    monkeypatch.setattr(
        rb, "_skill_refs", lambda ids: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    bundle = rb.bind_runtime_assets(run_id="run-2", skill_ids=["s1"])
    assert bundle.partial is True


def test_bundle_is_retrievable_by_run_id(monkeypatch):
    monkeypatch.setattr(rb, "_prompt_refs", lambda: [])
    monkeypatch.setattr(rb, "_ontology_refs", lambda: [])
    monkeypatch.setattr(rb, "_skill_refs", lambda ids: [_ref(C.ASSET_SKILL, "s1")])

    bundle = rb.bind_runtime_assets(run_id="run-3", skill_ids=["s1"])
    assert rb.resolve_bundle_for_run("run-3").bundle_id == bundle.bundle_id
    assert rb.get_bundle(bundle.bundle_id) is not None


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

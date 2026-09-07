"""P0: paths, refs, links, archive safety, store, index, resolver, view."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from core.capabilities import archive, junction, paths, registry, store, view
from core.capabilities.errors import IntegrityFailed
from core.capabilities.ref import ResourceRef, cloud_ref, local_ref, profile_id
from core.capabilities.resolver import Candidate, resolve


def _zip(files: dict, root: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, body in files.items():
            zf.writestr(f"{root}/{rel}" if root else rel, body)
    return buf.getvalue()


# ── paths & refs ──────────────────────────────────────────────────────


def test_store_is_disabled_without_root(monkeypatch):
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    assert not paths.capabilities_enabled()
    with pytest.raises(paths.CapabilityStoreDisabled):
        paths.require_root()


def test_kind_roots_and_segments(caps_root):
    assert paths.kind_root("skill") == caps_root / "skills"
    assert (
        paths.component_dir("plugin", "local", "x", "r1")
        == caps_root / "plugins" / "local" / "x" / "r1"
    )
    assert paths.mcp_json_path() == caps_root / "mcp.json"
    for bad in ("", "..", "a/b", "a\\b", ".hidden", "CON", "nul.txt", "a b"):
        with pytest.raises(ValueError):
            paths.safe_segment(bad)
    assert paths.revision_for_hash("ab" * 32) == "abababababab"


def test_resource_ref_roundtrip():
    ref = cloud_ref("https://Cloud.Example:8443/api/", "skill", "word-editing", scope="shared")
    assert ref.issuer.startswith("cloud_") and "/" not in ref.issuer
    assert ResourceRef.parse(str(ref)) == ref
    assert ResourceRef.from_dict(ref.to_dict()) == ref
    assert local_ref("agent", "helper").issuer == "local"
    with pytest.raises(ValueError):
        ResourceRef("x", "y", "nope", "z")
    p1 = profile_id("https://cloud.example", "u-1")
    assert p1.startswith("p_") and len(p1) == 34
    assert p1 == profile_id("https://CLOUD.example:443/", "u-1")
    assert p1 != profile_id("http://cloud.example", "u-1")
    assert p1 != profile_id("https://cloud.example", "u-2")


# ── links ──────────────────────────────────────────────────────────────


def test_directory_links_are_links_not_copies(tmp_path):
    target = tmp_path / "store" / "a"
    target.mkdir(parents=True)
    (target / "f.txt").write_text("1")
    link = tmp_path / "view" / "a"
    assert junction.ensure_directory_link(link, target, allowed_roots=[tmp_path / "store"]) is True
    assert junction.is_directory_link(link)
    assert (link / "f.txt").read_text() == "1"
    assert junction.read_directory_link(link) == Path(target.resolve())
    assert junction.ensure_directory_link(link, target, allowed_roots=[tmp_path / "store"]) is False

    other = tmp_path / "store" / "b"
    other.mkdir()
    assert junction.ensure_directory_link(link, other, allowed_roots=[tmp_path / "store"]) is True
    assert junction.read_directory_link(link) == Path(other.resolve())

    junction.remove_directory_link(link)
    assert not link.exists() and other.is_dir()


def test_link_refuses_targets_outside_roots_and_real_dirs(tmp_path):
    inside = tmp_path / "store" / "a"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(junction.LinkTargetOutsideRoot):
        junction.create_directory_link(
            tmp_path / "v" / "a", outside, allowed_roots=[tmp_path / "store"]
        )
    real = tmp_path / "v" / "real"
    real.mkdir(parents=True)
    with pytest.raises(junction.LinkError):
        junction.ensure_directory_link(real, inside, allowed_roots=[tmp_path / "store"])
    with pytest.raises(junction.LinkError):
        junction.remove_directory_link(real)
    assert real.is_dir()


# ── archive safety ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["../x", "/abs", "C:evil", "a:stream", "a\\b", "//unc/share", "CON/x", "a/../../b"],
)
def test_archive_rejects_unsafe_members(name):
    with pytest.raises(IntegrityFailed):
        archive.inspect_zip(_zip({name: "x", "SKILL.md": "s"}))


def test_archive_rejects_case_collisions_and_strips_root(tmp_path):
    with pytest.raises(IntegrityFailed):
        archive.inspect_zip(_zip({"A.txt": "1", "a.txt": "2"}))
    _zf, entries, strip = archive.inspect_zip(
        _zip({"SKILL.md": "s", "scripts/a.py": "p"}, root="my-skill")
    )
    assert strip == "my-skill" and {e.relative_path for e in entries} == {
        "SKILL.md",
        "scripts/a.py",
    }
    written = archive.extract_zip(
        _zip({"SKILL.md": "s", "scripts/a.py": "p"}, root="my-skill"), tmp_path / "out"
    )
    assert sorted(written) == ["SKILL.md", "scripts/a.py"]
    assert (tmp_path / "out" / "scripts" / "a.py").read_text() == "p"


# ── store ─────────────────────────────────────────────────────────────


def test_store_publishes_immutable_revisions(caps_root):
    comp = store.write_from_zip("skill", "p_1", "word", "r1", _zip({"SKILL.md": "v1"}, root="word"))
    assert comp.path == caps_root / "skills" / "p_1" / "word" / "r1"
    assert comp.entry_file.read_text() == "v1"
    assert store.inventory(comp) == ["SKILL.md"]
    # same revision again is a no-op, content stays what was first published
    store.write_from_zip("skill", "p_1", "word", "r1", _zip({"SKILL.md": "tampered"}, root="word"))
    assert comp.entry_file.read_text() == "v1"
    store.write_from_files("skill", "local", "word", "r9", {"SKILL.md": "local"})
    assert {(c.profile, c.key, c.revision) for c in store.iter_components("skill")} == {
        ("p_1", "word", "r1"),
        ("local", "word", "r9"),
    }
    with pytest.raises(IntegrityFailed):
        store.write_from_files("skill", "local", "bad", "r1", {"README.md": "no entry"})
    assert not any(paths.staging_root().iterdir())
    assert store.remove_key("skill", "p_1", "word") == 1
    assert store.get("skill", "p_1", "word", "r1") is None


def test_store_flat_import_candidates(caps_root):
    (caps_root / "skills" / "dropped").mkdir(parents=True)
    (caps_root / "skills" / "dropped" / "SKILL.md").write_text("x")
    (caps_root / "skills" / "local").mkdir()
    assert [p.name for p in store.flat_import_candidates("skill")] == ["dropped"]


# ── registry ──────────────────────────────────────────────────────────


def test_registry_lifecycle(index_db):
    ref = cloud_ref("https://c", "skill", "word", scope="shared")
    inst = registry.upsert(profile_id="p_1", ref=ref, display_name="Word", content_hash="a" * 64)
    assert inst.state == "pending" and inst.install_id == "skill:p_1:word"
    tx = registry.begin_transaction(inst.install_id, 1)
    registry.advance_transaction(tx, "published", inventory=["SKILL.md"])
    assert registry.open_transactions()[0]["phase"] == "published"
    ready = registry.set_state(inst.install_id, "ready", resolved_revision="aaaaaaaaaaaa")
    assert ready.ready and ready.generation == 1
    registry.advance_transaction(tx, "committed")
    assert registry.open_transactions() == []

    # new cloud content: still ready on the old revision, flagged update_available
    again = registry.upsert(profile_id="p_1", ref=ref, content_hash="b" * 64)
    assert again.state == "ready" and again.payload.get("update_available") is True
    updated = registry.set_state(again.install_id, "ready", resolved_revision="bbbbbbbbbbbb")
    assert not updated.payload.get("update_available")
    assert updated.payload["resolved_content_hash"] == "b" * 64

    registry.set_preference("skill", "word", inst.install_id, chosen_by="u-1")
    assert registry.preferences("skill", user_id="u-1") == {"word": inst.install_id}
    assert registry.clear_preference("skill", "word", user_id="u-1") is True

    registry.set_components("plugin:p_1:pack", {inst.install_id: True})
    assert registry.owners_of(inst.install_id) == ["plugin:p_1:pack"]
    assert registry.mark_removed(inst.install_id)
    assert registry.list_installations(kind="skill") == []
    assert registry.list_installations(kind="skill", include_removed=True)[0].state == "removed"
    assert registry.delete(inst.install_id) and registry.owners_of(inst.install_id) == []


# ── resolver ──────────────────────────────────────────────────────────


def _cand(iid, name, profile, *, path="p", usable=True, account=False, h=None, source="cloud"):
    return Candidate(
        install_id=iid,
        runtime_name=name,
        kind="skill",
        profile=profile,
        source=source,
        path=Path(path) if path else None,
        usable=usable,
        account_level=account,
        content_hash=h,
    )


def test_resolver_order_preference_request_account_global():
    builtin = _cand("skill:builtin:x", "x", "builtin", account=False, h="1", source="builtin")
    cloud = _cand("skill:p_1:x", "x", "p_1", account=True, h="2")
    local = _cand("skill:local:x", "x", "local", account=True, h="3", source="local")

    r = resolve("skill", [builtin, cloud])
    assert r.chosen["x"] is cloud and r.reasons["x"] == "account_unique"
    assert r.shadowed["x"] == [builtin]

    r = resolve("skill", [builtin, cloud, local])
    assert "x" in r.conflicts and r.reasons["x"] == "account_conflict"

    r = resolve("skill", [builtin, cloud, local], preferences={"x": local.install_id})
    assert r.chosen["x"] is local and r.reasons["x"] == "preference"

    r = resolve("skill", [builtin, cloud, local], requested={cloud.install_id})
    assert r.chosen["x"] is cloud and r.reasons["x"] == "requested"

    r = resolve("skill", [builtin])
    assert r.chosen["x"] is builtin and r.reasons["x"] == "global_unique"

    pending = _cand("skill:p_1:x", "x", "p_1", path=None, usable=False, account=True)
    r = resolve("skill", [builtin, pending])
    assert r.chosen["x"] is builtin  # not-yet-downloaded cloud copy never blocks the shipped one

    r = resolve("skill", [builtin, pending], preferences={"x": pending.install_id})
    assert "x" not in r.chosen and r.reasons["x"] == "preferred_not_ready"


def test_resolver_identical_content_is_not_a_conflict():
    a = _cand("skill:builtin:x", "x", "builtin", account=False, h="same", source="builtin")
    b = _cand("skill:p_1:x", "x", "p_1", account=True, h="same")
    c = _cand("skill:local:x", "x", "local", account=True, h="same", source="local")
    r = resolve("skill", [a, b, c])
    assert r.chosen["x"] is c and r.reasons["x"] == "account_identical"
    r = resolve("skill", [a, _cand("skill:p_2:x", "x", "p_2", h="same")])
    assert r.chosen["x"].profile == "p_2" and r.reasons["x"] == "global_identical"


# ── view ──────────────────────────────────────────────────────────────


def test_view_builder_links_relinks_removes_and_blocks(tmp_path):
    store_root = tmp_path / "store"
    for name in ("a1", "a2", "b1"):
        (store_root / name).mkdir(parents=True)
    view_dir = tmp_path / "view"
    (view_dir / "real").mkdir(parents=True)  # legacy user data
    (view_dir / "stray.txt").write_text("x")

    r = view.build_view(
        view_dir, {"a": store_root / "a1", "b": store_root / "b1"}, allowed_roots=[store_root]
    )
    assert r.linked == ["a", "b"] and r.foreign == ["real"] and not r.blocked
    assert view.link_targets(view_dir) == {
        "a": (store_root / "a1").resolve(),
        "b": (store_root / "b1").resolve(),
    }

    r = view.build_view(
        view_dir, {"a": store_root / "a2", "real": store_root / "b1"}, allowed_roots=[store_root]
    )
    assert r.relinked == ["a"] and r.removed == ["b"]
    assert (
        "real" in r.blocked
        and (view_dir / "real").is_dir()
        and not junction.is_directory_link(view_dir / "real")
    )
    assert (view_dir / "stray.txt").exists()

    r = view.build_view(view_dir, {"a": tmp_path / "outside"}, allowed_roots=[store_root])
    assert "a" in r.blocked  # nonexistent / outside target reported, not copied


def test_archive_preserves_safe_hidden_package_assets(tmp_path):
    archive.extract_zip(_zip({"SKILL.md": "s", ".hidden/x": "asset"}), tmp_path / "out")
    assert (tmp_path / "out" / ".hidden" / "x").read_text() == "asset"

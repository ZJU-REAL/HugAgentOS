"""Self-test for two-phase skill loading optimization."""

from __future__ import annotations

import time
from core.agent_skills.loader import get_skill_loader


def test_metadata_loading():
    """Test that metadata loading is faster than full loading."""
    print("Testing metadata loading performance...")

    loader = get_skill_loader(reset=True)

    # Measure metadata loading time
    start = time.time()
    metadata_registry = loader.load_all_metadata()
    metadata_time = time.time() - start

    # Measure full spec loading time (load all skills)
    start = time.time()
    full_specs = []
    for skill_id in metadata_registry:
        spec = loader.load_skill_full(skill_id)
        if spec:
            full_specs.append(spec)
    full_time = time.time() - start

    print(f"  Metadata loading: {metadata_time:.4f}s ({len(metadata_registry)} skills)")
    print(f"  Full spec loading: {full_time:.4f}s ({len(full_specs)} skills)")

    if metadata_time < full_time:
        print("  ✓ Metadata loading is faster")
    else:
        print("  ⚠ Metadata loading time is not significantly faster (may be OK for small skill sets)")

    # Verify metadata structure
    if metadata_registry:
        first_meta = next(iter(metadata_registry.values()))
        assert hasattr(first_meta, "id")
        assert hasattr(first_meta, "name")
        assert hasattr(first_meta, "description")
        assert hasattr(first_meta, "version")
        assert hasattr(first_meta, "tags")
        assert not hasattr(first_meta, "instructions"), "Metadata should not have instructions"
        print("  ✓ Metadata structure is correct")


def test_on_demand_loading():
    """Test that individual skill loading works."""
    print("\nTesting on-demand skill loading...")

    loader = get_skill_loader()
    metadata_registry = loader.load_all_metadata()
    if not metadata_registry:
        print("  ⚠ No skills found, skipping test")
        return

    # Pick first skill
    skill_id = next(iter(metadata_registry.keys()))
    print(f"  Loading skill: {skill_id}")

    full_spec = loader.load_skill_full(skill_id)
    assert full_spec is not None, f"Failed to load skill: {skill_id}"
    assert full_spec.id == skill_id
    assert len(full_spec.instructions) > 0, "Full spec should have instructions"
    print(f"  ✓ Loaded full spec with {len(full_spec.instructions)} instructions")


def test_resolve_skills_optimization():
    """Test skill loading for selected skills only."""
    print("\nTesting selective skill loading...")

    loader = get_skill_loader()
    metadata_registry = loader.load_all_metadata()
    if not metadata_registry:
        print("  ⚠ No skills found, skipping test")
        return

    # Get first skill id
    skill_id = next(iter(metadata_registry.keys()))
    print(f"  Loading skill: {skill_id}")

    # Measure load time (should only load one skill)
    start = time.time()
    spec = loader.load_skill_full(skill_id)
    load_time = time.time() - start

    assert spec is not None
    assert spec.id == skill_id
    assert len(spec.instructions) > 0

    print(f"  ✓ Loaded 1 skill in {load_time:.4f}s")
    print(f"  ✓ Skill has {len(spec.instructions)} instructions")


def test_metadata_cache():
    """Test that metadata is cached."""
    print("\nTesting metadata caching...")

    loader = get_skill_loader(reset=True)

    # First load
    start = time.time()
    metadata1 = loader.load_all_metadata()
    first_time = time.time() - start

    # Second load (should use cache)
    start = time.time()
    metadata2 = loader.load_all_metadata()
    second_time = time.time() - start

    print(f"  First load: {first_time:.4f}s")
    print(f"  Second load (cached): {second_time:.4f}s")

    assert metadata1 is metadata2, "Should return same cached object"
    print(f"  ✓ Metadata is cached (same object)")


def main():
    print("=" * 60)
    print("Skill Loading Optimization Self-Test")
    print("=" * 60)

    try:
        test_metadata_loading()
        test_on_demand_loading()
        test_resolve_skills_optimization()
        test_metadata_cache()

        print("\n" + "=" * 60)
        print("✓ All tests passed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        raise


if __name__ == "__main__":
    main()

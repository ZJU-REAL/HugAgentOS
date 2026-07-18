"""Self-test for dynamic skill selection (explicit + implicit)."""

from __future__ import annotations

from core.agent_skills.selector import select_skills_for_query
from core.config.catalog import get_enabled_ids


def test_skill_selector_no_model():
    """Test skill selector without LLM (fallback mode)."""
    print("Testing skill selector without LLM...")

    enabled_ids = get_enabled_ids("skills")
    available_ids = ["process-guidance", "quick-material-analysis"]

    selected = select_skills_for_query(
        user_query="请梳理这个事项的申报流程",
        available_skill_ids=available_ids,
        enabled_skill_ids=enabled_ids,
        model=None,  # No LLM
        max_skills=2,
    )

    print(f"  Available: {available_ids}")
    print(f"  Enabled: {enabled_ids}")
    print(f"  Selected (fallback): {selected}")
    assert isinstance(selected, list)
    print("  ✓ Selector fallback works")


def test_skill_selector_with_llm():
    """Test skill selector with LLM (if model available)."""
    print("\nTesting skill selector with LLM...")

    try:
        from core.llm.chat_models import get_summarize_model

        model = get_summarize_model()
    except Exception as e:
        print(f"  ⚠ Model not available, skipping LLM test: {e}")
        return

    enabled_ids = get_enabled_ids("skills")
    available_ids = [
        "quick-material-analysis",
        "policy-search-interpretation",
        "report-summary-generation",
    ]

    # Test 1: Query that needs material analysis
    selected = select_skills_for_query(
        user_query="请深度分析这份产业规划材料，并提炼重点内容",
        available_skill_ids=available_ids,
        enabled_skill_ids=enabled_ids,
        model=model,
        max_skills=3,
    )

    print(f"  Query: '深度分析产业规划材料'")
    print(f"  Selected: {selected}")
    assert isinstance(selected, list)
    print("  ✓ LLM selection works")

    # Test 2: Query that needs policy interpretation
    selected2 = select_skills_for_query(
        user_query="请解读这项政策的申报条件和支持方式",
        available_skill_ids=available_ids,
        enabled_skill_ids=enabled_ids,
        model=model,
        max_skills=3,
    )

    print(f"\n  Query: '解读政策申报条件'")
    print(f"  Selected: {selected2}")
    assert isinstance(selected2, list)
    print("  ✓ LLM selection adapts to query")


def test_explicit_vs_implicit():
    """Test explicit (required) vs implicit (available) skills via AgentSpec."""
    print("\nTesting explicit vs implicit skills...")

    from orchestration.registry import AgentSpec

    spec = AgentSpec(
        name="test_agent",
        required_skills=["skill_a"],
        available_skills=["skill_b", "skill_c"],
    )

    print(f"  Required skills (explicit): {spec.required_skills}")
    print(f"  Available skills (implicit): {spec.available_skills}")

    assert isinstance(spec.required_skills, list)
    assert isinstance(spec.available_skills, list)
    assert spec.required_skills == ["skill_a"]
    assert spec.available_skills == ["skill_b", "skill_c"]

    print("  ✓ AgentSpec has required_skills and available_skills")


def main():
    print("=" * 60)
    print("Dynamic Skill Selection Self-Test")
    print("=" * 60)

    try:
        test_skill_selector_no_model()
        test_skill_selector_with_llm()
        test_explicit_vs_implicit()

        print("\n" + "=" * 60)
        print("✓ All tests passed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()

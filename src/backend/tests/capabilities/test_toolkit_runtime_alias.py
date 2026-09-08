"""Real AgentScope rendering and loading must use resolved runtime names."""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.tool import Toolkit
from agentscope.state import AgentState
from core.agent_skills.loader import MultiSourceSkillLoader
from core.capabilities import registry, runtime, skills, store
from core.capabilities.errors import IntegrityFailed, PermissionDenied
from core.llm.agent_factory import create_agent_executor
from core.llm.tool_collector import ToolCollector
from core.llm.tools.skill_tool import register_sandboxed_view_text_file
from core.services.desktop_capability_protocol import skill_content_hash


def factory_template():
    # Render the exact factory template with the real AgentScope Toolkit.
    tree = ast.parse(inspect.getsource(create_agent_executor))
    return next(
        ast.literal_eval(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_SKILL_INSTRUCTION_TEMPLATE" for t in n.targets
        )
    )


@pytest.fixture
def frozen(index_db, caps_root, monkeypatch):
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(
        "core.agent_skills.loader.get_skill_loader",
        lambda: SimpleNamespace(load_all_metadata=lambda: {}),
    )

    def create(names, physical=False):
        for alias, body in names.items():
            md = (
                "---\nname: original-cloud-name\ndescription: Synthetic marker reader\n---\n" + body
            )
            skills.publish_local_skill(
                alias,
                files={"SKILL.md": md},
                content_hash=skill_content_hash(md, {}),
                owner_user_id="owner",
            )
        prepared = runtime.prepare("run-alias", "owner", skill_ids=list(names))
        loader = runtime.frozen_loader(prepared)
        if physical:
            original_get_dir = loader.get_skill_dir
            loader.get_skill_dir = lambda sid: str(Path(original_get_dir(sid)).resolve())
        collector = ToolCollector()
        assert loader.register_skills_to_toolkit(collector, list(names)) == len(names)
        return prepared, loader, collector

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("physical", [False, True])
async def test_real_factory_prompt_and_native_registry_preserve_two_custom_aliases(
    frozen, physical
):
    prepared, loader, collector = frozen(
        {"mine-one": "MARKER_ONE", "mine-two": "MARKER_TWO"}, physical=physical
    )
    toolkit = Toolkit(
        skills_or_loaders=collector.skill_loaders, skill_instruction_template=factory_template()
    )
    prompt = await toolkit.get_skill_instructions()
    native = await toolkit._get_available_skills()
    assert set(native) == {"mine-one", "mine-two"}
    for alias, marker in (("mine-one", "MARKER_ONE"), ("mine-two", "MARKER_TWO")):
        assert "- `" + alias + "`" in prompt
        assert prepared.bindings[alias]["revision"] not in prompt
        assert native[alias].name == alias and native[alias].dir == "/workspace/skills/" + alias
        assert native[alias].markdown == marker
        loaded = await toolkit.builtin_skill_viewer.tool(alias, AgentState())
        assert "\n".join(block.text for block in loaded.content) == marker
        assert "name: original-cloud-name" in (prepared.view_dir / alias / "SKILL.md").read_text()
    assert "- `original-cloud-name`" not in prompt


@pytest.mark.asyncio
async def test_native_projection_is_detached_and_custom_alias_reads_frozen_bytes(frozen):
    prepared, loader, collector = frozen({"custom-local": "FROZEN_CONTENT"})
    toolkit = Toolkit(
        skills_or_loaders=collector.skill_loaders, skill_instruction_template=factory_template()
    )
    native = await toolkit._get_available_skills()
    assert "custom-local" in native
    native["custom-local"].name = "poison"
    native["custom-local"].dir = "/wrong"
    again = await toolkit._get_available_skills()
    assert again["custom-local"].name == "custom-local"
    assert again["custom-local"].dir == "/workspace/skills/custom-local"
    register_sandboxed_view_text_file(collector, [loader.get_skill_dir("custom-local")], loader)
    tool = collector.get_tool("view_text_file")._func
    response = await tool("/workspace/skills/custom-local/SKILL.md")
    assert "FROZEN_CONTENT" in "\n".join(block.text for block in response.content)
    denied = await tool(
        "/workspace/skills/" + prepared.bindings["custom-local"]["revision"] + "/SKILL.md"
    )
    assert "Access denied" in "\n".join(block.text for block in denied.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revoke", "corrupt"])
async def test_native_projection_rechecks_frozen_authorization_and_hash_before_cached_read(
    frozen, mutation
):
    prepared, loader, collector = frozen({"mine": "CACHED_BODY"})
    toolkit = Toolkit(
        skills_or_loaders=collector.skill_loaders, skill_instruction_template=factory_template()
    )
    await toolkit.get_skill_instructions()
    if mutation == "revoke":
        registry.set_enabled("skill:local:mine", False)
    else:
        (prepared.view_dir / "mine" / "SKILL.md").write_text("changed")
    with pytest.raises((IntegrityFailed, PermissionDenied)):
        await toolkit.get_skill_instructions()


def test_explicit_skill_hint_uses_authorized_alias_instead_of_store_revision(frozen, monkeypatch):
    from orchestration.workflow import _build_skill_injection

    prepared, loader, collector = frozen({"chosen-local-alias": "FROZEN_BODY"})
    physical = str((prepared.view_dir / "chosen-local-alias").resolve())
    assert Path(physical).name == prepared.bindings["chosen-local-alias"]["revision"]
    # The global capability-store loader returns this real revision directory.
    monkeypatch.setattr(loader, "get_skill_dir", lambda sid: physical)
    monkeypatch.setattr("core.agent_skills.loader.get_skill_loader", lambda: loader)
    hint = _build_skill_injection({"skill_id": "chosen-local-alias", "skill_name": "Chosen"})
    assert "/workspace/skills/chosen-local-alias/SKILL.md" in hint["content"]
    assert prepared.bindings["chosen-local-alias"]["revision"] not in hint["content"]

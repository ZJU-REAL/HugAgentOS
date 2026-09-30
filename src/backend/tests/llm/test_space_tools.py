"""Space tool registration keeps the public surface and permission gates aligned."""

from core.llm.tools._state import ReadStateTracker


class Toolkit:
    def __init__(self):
        self.functions = {}

    def register_tool_function(self, fn, **kwargs):
        self.functions[fn.__name__] = fn


def test_space_registration_exposes_only_space_file_tools():
    from core.llm.tools.space_tools import register_space_tools
    from core.llm.tool_permissions import builtin_tool_permission

    toolkit = Toolkit()
    register_space_tools(toolkit, user_id="alice", state=ReadStateTracker())
    assert set(toolkit.functions) - {"space_list_team_files"} == {
        "space_list_myspace_files",
        "space_stage_myspace_file",
        "space_create_folder",
        "space_move",
        "space_delete",
    }
    for name in ("space_create_folder", "space_move", "space_delete"):
        assert builtin_tool_permission(name) is not None
    for name in ("Delete", "Move", "CreateFolder"):
        assert builtin_tool_permission(name) is None


def test_space_tools_require_a_user_identity():
    from core.llm.tools.space_tools import register_space_tools

    toolkit = Toolkit()
    register_space_tools(toolkit, user_id=None, state=ReadStateTracker())
    assert toolkit.functions == {}


def test_space_tool_recovery_preserves_read_and_mutation_policies():
    from core.services.tool_effect_ledger import build_default_tool_recovery_registry

    registry = build_default_tool_recovery_registry()
    for name in (
        "space_list_myspace_files",
        "space_stage_myspace_file",
    ):
        assert registry.resolve(name).policy == "replay_safe"
    for name in ("space_create_folder", "space_move", "space_delete"):
        assert registry.resolve(name).policy == "never_replay"


def test_real_tool_collector_keeps_space_mutations_governed():
    from core.llm.tool_collector import ToolCollector
    from core.llm.tools.space_tools import register_space_tools

    collector = ToolCollector()
    register_space_tools(collector, user_id="alice", state=ReadStateTracker())
    names = {tool.name for tool in collector.function_tools}
    assert "space_move" in names
    assert "space_delete" in names
    assert "space_create_folder" in names
    assert set(collector.permission_specs) == {
        "space_move", "space_delete", "space_create_folder",
    }


def test_personal_space_registration_works_without_the_organization_extension(monkeypatch):
    from core.llm.tools import myspace_tool
    from core.llm.tools.space_tools import register_space_tools

    monkeypatch.setattr(myspace_tool, "register_organization_tools", lambda *args: None)
    toolkit = Toolkit()
    register_space_tools(toolkit, user_id="alice", state=ReadStateTracker())
    assert set(toolkit.functions) == {
        "space_list_myspace_files", "space_stage_myspace_file",
        "space_create_folder", "space_move", "space_delete",
    }
